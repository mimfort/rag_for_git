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

from reviewer.index.refs import base_ref
from reviewer.retrieval.retriever import (
    ContextPack, _dedupe_overlapping, _is_test_path,
)

log = logging.getLogger(__name__)

RRF_K = 60
"""Константа RRF — та же, что в store.hybrid_search и TaskStore.search."""

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


def cap_block(item, max_chars: int):
    """Обрезать текст блока по границе строк, поправив end_line.

    Границу строк держим не ради красоты: as_context нумерует строки от
    start_line, а extract_context_paths требует в заголовке диапазон
    ':\\d+-\\d+'. Обрезка по середине строки сделала бы заголовок ложью, а
    усечение уже в as_context — вовсе съело бы заголовок и потеряло путь.

    Бюджет приходит из политики (CodeSectionLimits.chars_per_file), а не из
    модульной константы: доля на файл — часть файлового бюджета секции.
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


def _embed_pairs(embedder, queries: list[str]) -> list[tuple]:
    """Пары (запрос, вектор) одним батчем; при сбое — только первый подзапрос.

    Откат идёт по первому подзапросу намеренно: он и есть продакшн-запрос
    целиком, поэтому деградация возвращает сегодняшнее поведение, а не пустоту.
    """
    if not queries:
        return []
    try:
        return list(zip(queries, embedder.embed_queries(queries), strict=True))
    except Exception:  # noqa: BLE001 — квота Voyage кончилась, это штатный случай
        log.warning("multiquery: батч-эмбеддинг недоступен — откат на один запрос",
                    exc_info=True)
        try:
            return [(queries[0], embedder.embed_query(queries[0]))]
        except Exception:  # noqa: BLE001
            log.warning("multiquery: эмбеддинг запроса недоступен", exc_info=True)
            return []


def _run(store, repo: str, query: str, qvec, lim, bref: str) -> list:
    """Один прогон гибрида с ANN-префильтром — тем же, что в search_base."""
    hits = store.hybrid_search(
        repo, query_text=query, query_embedding=qvec,
        overlay_ref="__none__", changed_paths=[],
        top_k=lim.candidate_pool, candidates=lim.candidate_pool, base_ref=bref)
    return [h for h in hits
            if getattr(h, "bm25_hit", False)
            or (getattr(h, "ann_distance", None) is not None
                and h.ann_distance <= lim.ann_distance_max)]


def _graph_items(retriever, repo: str, merged: list, ceiling: int, hops: int,
                 branch: str, bref: str, hybrid_ids: set) -> list:
    """Graph-expansion один раз, от топа слитого списка. Fail-soft."""
    if retriever.graph is None or not merged:
        return []
    try:
        seeds = [item.node_id for item in merged[:ceiling]]
        expanded = retriever.graph.expand_detailed(repo, seeds, hops=hops, branch=branch)
        graph_ids = [row["id"] for row in expanded]
        fetched = {item.node_id: item for item in retriever.store.fetch_nodes(
            repo, graph_ids, "__none__", [], base_ref=bref)}
        return [fetched[node_id] for node_id in graph_ids
                if node_id in fetched and node_id not in hybrid_ids]
    except Exception:  # noqa: BLE001
        log.warning("multiquery: graph-expansion недоступен", exc_info=True)
        return []


def diversify_by_file(items: list, *, max_files: int, max_chunks_per_file: int) -> list:
    """Оставить не более max_chunks_per_file чанков на путь и не более max_files путей.

    Идёт по входному порядку и порядок выживших не меняет, поэтому приоритет
    «сначала hybrid, потом graph-only» и ранг RRF внутри файла сохраняются.

    Зовётся строго ПОСЛЕ _dedupe_overlapping: тот оставляет самый широкий чанк
    из вложенных, и обратный порядок удержал бы вложенный метод, выбросив
    охватывающий класс, — то есть ухудшил бы выдачу, а не улучшил.
    """
    per_file: dict[str, int] = {}
    kept: list = []
    for item in items:
        taken = per_file.get(item.path, 0)
        if taken >= max_chunks_per_file:
            continue
        if taken == 0 and len(per_file) >= max_files:
            continue
        per_file[item.path] = taken + 1
        kept.append(item)
    return kept


def search_multi(retriever, repo: str, queries: list[str], *, limits=None,
                 section_limits=None, hops: int = 1, branch: str = "",
                 include_tests: bool = False) -> ContextPack:
    """Мультизапросный ретрив по base-индексу ветки: N прогонов, RRF, обрезка.

    Реранкера и cliff-отсечки здесь нет — финальный ранкер RRF (см. докстринг
    модуля). Порядок «сначала hybrid, потом graph-only» сохранён из search_base:
    hybrid приоритетен, граф добавляет разнообразие.

    Бюджет выдачи файловый (PRI-256): не более section_limits.max_files
    различных путей. Операционный бюджет символов — max_files ×
    max_chunks_per_file × chars_per_file (его держит cap_block на ИСХОДНОМ
    тексте блока); section_limits.max_chars — лишь страховочный потолок после
    рендера.
    """
    from reviewer.policy.context_limits import CodebaseLimits, CodeSectionLimits
    lim = limits or CodebaseLimits()
    sec = section_limits or CodeSectionLimits()
    bref = base_ref(branch)
    # Дедуп на входе: search_multi — функция общего вида (зовётся и из эвала со
    # своими списками), а не только из build_subqueries, который уже дедуплицирует.
    # Порядок сохраняем — первый подзапрос обязан остаться первым (важно для
    # отката _embed_pairs при сбое батча).
    deduped_queries = list(dict.fromkeys(queries))
    runs: list[list] = []
    for query, qvec in _embed_pairs(retriever.embedder, deduped_queries):
        try:
            runs.append(_run(retriever.store, repo, query, qvec, lim, bref))
        except Exception:  # noqa: BLE001 — сбой одного прогона не роняет сборку
            log.warning("multiquery: прогон подзапроса не удался", exc_info=True)
    merged = rrf_merge(runs)
    hybrid_ids = {item.node_id for item in merged}
    items = [*merged, *_graph_items(retriever, repo, merged, lim.ceiling, hops,
                                    branch, bref, hybrid_ids)]
    if not include_tests:
        items = [item for item in items if not _is_test_path(item.path)]
    items = diversify_by_file(_dedupe_overlapping(items),
                              max_files=sec.max_files,
                              max_chunks_per_file=sec.max_chunks_per_file)
    return ContextPack(items=[cap_block(item, sec.chars_per_file) for item in items],
                       max_chars=sec.max_chars)
