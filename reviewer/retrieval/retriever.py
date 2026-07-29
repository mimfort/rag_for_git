from __future__ import annotations
from dataclasses import dataclass
import logging

from reviewer.index.refs import base_ref
from reviewer.retrieval.cliff import format_tail_note, select_by_cliff

log = logging.getLogger(__name__)


def _is_test_path(path: str) -> bool:
    """Путь относится к тестам: содержит сегмент ``tests`` или basename
    соответствует ``test_*.py`` / ``*_test.py``.
    """
    p = path.replace("\\", "/")
    parts = p.split("/")
    if "tests" in parts:
        return True
    base = parts[-1]
    return base.startswith("test_") or base.endswith("_test.py")


def _dedupe_overlapping(items: list) -> list:
    """Убрать вложенные дубли чанков.

    Чанк отбрасывается, если его диапазон ``[start_line, end_line]`` полностью
    вложен в диапазон другого удержанного чанка того же ``path`` (правило
    «оставить самый широкий»: класс уже включает текст своих методов). Чанки с
    одинаковым диапазоном и частично пересекающиеся сохраняются. Порядок
    выживших стабилен относительно входа.
    """
    def _nested_in(inner, outer) -> bool:
        return (
            outer.path == inner.path
            and outer.start_line <= inner.start_line
            and inner.end_line <= outer.end_line
            and (outer.start_line, outer.end_line) != (inner.start_line, inner.end_line)
        )

    kept: list = []
    for it in items:
        if any(_nested_in(it, k) for k in kept):
            continue
        kept = [k for k in kept if not _nested_in(k, it)]
        kept.append(it)
    return kept


def _select_degraded_context(hybrid_items: list, graph_items: list, ceiling: int) -> list:
    """Bounded fallback: hybrid приоритетен, graph даёт минимальное разнообразие."""
    if ceiling <= 0:
        return []
    if not hybrid_items:
        return graph_items[:ceiling]
    if ceiling == 1:
        return hybrid_items[:1]
    if len(hybrid_items) < ceiling:
        free = ceiling - len(hybrid_items)
        return [*hybrid_items, *graph_items[:free]]
    if graph_items:
        return [*hybrid_items[:ceiling - 1], graph_items[0]]
    return hybrid_items[:ceiling]


@dataclass
class ContextPack:
    items: list
    max_chars: int = 0
    max_tokens: int = 0
    tail_meta: object = None        # TailMeta | None (PRI-202); ленивая заметка о хвосте

    def as_context(self, line_numbers: bool = False) -> str:
        parts = []
        for it in self.items:
            header = f"// {it.node_id} ({it.path}:{it.start_line}-{it.end_line})"
            if line_numbers:
                body = "\n".join(
                    f"{it.start_line + i:>5} | {line}"
                    for i, line in enumerate(it.text.split("\n"))
                )
            else:
                body = it.text
            parts.append(f"{header}\n{body}")
        text = "\n\n".join(parts)
        limit = 0
        if self.max_chars > 0:
            limit = self.max_chars
        elif self.max_tokens > 0:
            limit = self.max_tokens * 4
        if limit > 0 and len(text) > limit:
            text = text[:limit] + "\n[...truncated]"
        note = format_tail_note(self.tail_meta) if self.tail_meta is not None else None
        if note:
            text = f"{text}\n\n{note}" if text else note
        return text


class Retriever:
    def __init__(self, store, graph, embedder, reranker, *,
                 max_context_chars: int = 0):
        self.store, self.graph = store, graph
        self.embedder, self.reranker = embedder, reranker
        self.max_context_chars = max_context_chars

    def retrieve(self, repo, query, changed_node_ids, overlay_ref, changed_paths,
                 top_k=15, candidates=50, *, branch="") -> ContextPack:
        bref = base_ref(branch)
        qvec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(
            repo, query_text=query, query_embedding=qvec, overlay_ref=overlay_ref,
            changed_paths=changed_paths, top_k=candidates, candidates=candidates,
            base_ref=bref)
        related_ids = self.graph.expand(repo, changed_node_ids, hops=2, branch=branch)
        related = self.store.fetch_nodes(repo, list(related_ids), overlay_ref,
                                         changed_paths, base_ref=bref)
        hit_ids = {h.node_id for h in hits}
        graph_new = [it for it in related if it.node_id not in hit_ids]
        merged: dict[str, object] = {}
        for it in [*hits, *related]:
            merged.setdefault(it.node_id, it)
        if len(merged) <= 3 or (len(merged) <= top_k and not graph_new):
            return ContextPack(items=list(merged.values()),
                               max_chars=self.max_context_chars)
        ranked = self.reranker.rerank(query, list(merged.values()), top_k=top_k)
        return ContextPack(items=ranked, max_chars=self.max_context_chars)

    def search_base(self, repo, query, *, limits=None, hops=1, ceiling_override=None,
                    branch="", include_tests=False) -> ContextPack:
        """Гибрид-поиск по base-индексу ветки без PR-сессии — для /solve-task (PRI-202).

        ANN-префильтр (BM25-aware) → rerank_scored → cliff-отсечка. Граф и
        реранкер fail-soft: hybrid приоритетен, graph сохраняет разнообразие.
        """
        from reviewer.policy.context_limits import CodebaseLimits
        lim = limits or CodebaseLimits()
        ceiling = ceiling_override or lim.ceiling
        bref = base_ref(branch)
        qvec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(
            repo, query_text=query, query_embedding=qvec,
            overlay_ref="__none__", changed_paths=[],
            top_k=lim.candidate_pool, candidates=lim.candidate_pool, base_ref=bref)
        # ANN-префильтр: оставляем лексические хиты и близкие по вектору; далёкий не-BM25 шум режем
        hits = [h for h in hits if getattr(h, "bm25_hit", False)
                or (getattr(h, "ann_distance", None) is not None
                    and h.ann_distance <= lim.ann_distance_max)]
        hybrid_ids = {hit.node_id for hit in hits}
        graph_only_ids: list[str] = []
        merged: dict[str, object] = {hit.node_id: hit for hit in hits}
        if self.graph is not None and hits:
            try:
                seeds = [hit.node_id for hit in hits[:ceiling]]
                expanded = self.graph.expand_detailed(repo, seeds, hops=hops, branch=branch)
                graph_ids = [row["id"] for row in expanded]
                fetched = self.store.fetch_nodes(repo, graph_ids, "__none__", [], base_ref=bref)
                fetched_by_id = {item.node_id: item for item in fetched}
                related = [fetched_by_id[node_id] for node_id in graph_ids
                           if node_id in fetched_by_id]
                for item in related:
                    if item.node_id not in hybrid_ids:
                        graph_only_ids.append(item.node_id)
                    merged.setdefault(item.node_id, item)
            except Exception:
                log.warning("search_base: graph-expansion недоступен", exc_info=True)
        items = list(merged.values())
        if not include_tests:
            items = [item for item in items if not _is_test_path(item.path)]
        items = _dedupe_overlapping(items)
        graph_only_id_set = set(graph_only_ids)
        hybrid_items = [item for item in items if item.node_id in hybrid_ids]
        graph_items = [item for item in items if item.node_id in graph_only_id_set]
        items = [*hybrid_items, *graph_items]
        if len(items) <= lim.floor:
            selected = (
                _select_degraded_context(hybrid_items, graph_items, ceiling)
                if len(items) > ceiling
                else items
            )
            return ContextPack(items=selected, max_chars=self.max_context_chars)
        if self.reranker is None:
            selected = _select_degraded_context(hybrid_items, graph_items, ceiling)
            return ContextPack(items=selected, max_chars=self.max_context_chars)
        try:
            scored = self.reranker.rerank_scored(query, items)
        except Exception:
            log.warning("search_base: rerank недоступен — deterministic fallback", exc_info=True)
            selected = _select_degraded_context(hybrid_items, graph_items, ceiling)
            return ContextPack(items=selected, max_chars=self.max_context_chars)
        kept, tail_meta = select_by_cliff(
            scored, floor_n=lim.floor, ceiling_n=ceiling,
            ratio=lim.ratio, abs_floor=lim.abs_floor)
        return ContextPack(items=kept, max_chars=self.max_context_chars, tail_meta=tail_meta)
