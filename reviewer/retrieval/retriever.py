from __future__ import annotations
from dataclasses import dataclass
import logging

log = logging.getLogger(__name__)


@dataclass
class ContextPack:
    items: list
    max_chars: int = 0
    max_tokens: int = 0

    def as_context(self) -> str:
        parts = []
        for it in self.items:
            parts.append(f"// {it.node_id} ({it.path}:{it.start_line}-{it.end_line})\n{it.text}")
        text = "\n\n".join(parts)
        limit = 0
        if self.max_chars > 0:
            limit = self.max_chars
        elif self.max_tokens > 0:
            limit = self.max_tokens * 4
        if limit > 0 and len(text) > limit:
            text = text[:limit] + "\n[...truncated]"
        return text


class Retriever:
    def __init__(self, store, graph, embedder, reranker, *,
                 max_context_chars: int = 0):
        self.store, self.graph = store, graph
        self.embedder, self.reranker = embedder, reranker
        self.max_context_chars = max_context_chars

    def retrieve(self, repo, query, changed_node_ids, overlay_ref, changed_paths,
                 top_k=15, candidates=50) -> ContextPack:
        qvec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(
            repo, query_text=query, query_embedding=qvec, overlay_ref=overlay_ref,
            changed_paths=changed_paths, top_k=candidates, candidates=candidates)
        related_ids = self.graph.expand(repo, changed_node_ids, hops=2)
        related = self.store.fetch_nodes(repo, list(related_ids), overlay_ref, changed_paths)
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

    def search_base(self, repo, query, top_k=10, candidates=50) -> ContextPack:
        """Гибрид-поиск по base-индексу без PR-сессии — для /solve-task.

        Зеркало :meth:`retrieve`, но base-only и сидинг графа от хитов:
        ``changed_paths=[]`` + несуществующий ``overlay_ref="__none__"`` → WHERE отбирает
        только base-строки. graph-expansion идёт от топ-хитов (а не от changed-файлов),
        затем rerank. Граф и реранкер fail-soft.
        """
        qvec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(
            repo, query_text=query, query_embedding=qvec,
            overlay_ref="__none__", changed_paths=[],
            top_k=candidates, candidates=candidates)
        merged: dict[str, object] = {}
        for h in hits:
            merged.setdefault(h.node_id, h)
        graph_new = False
        if self.graph is not None and hits:
            try:
                seeds = [h.node_id for h in hits[:top_k]]
                related_ids = self.graph.expand(repo, seeds, hops=1)
                related = self.store.fetch_nodes(repo, list(related_ids), "__none__", [])
                for it in related:
                    if it.node_id not in merged:
                        merged[it.node_id] = it
                        graph_new = True
            except Exception:
                log.warning("search_base: graph-expansion недоступен", exc_info=True)
        items = list(merged.values())
        if self.reranker is None or len(items) <= 3 or (len(items) <= top_k and not graph_new):
            return ContextPack(items=items[:top_k], max_chars=self.max_context_chars)
        try:
            items = self.reranker.rerank(query, items, top_k=top_k)
        except Exception:
            log.warning("search_base: rerank недоступен — RRF-порядок", exc_info=True)
            items = items[:top_k]
        return ContextPack(items=items, max_chars=self.max_context_chars)
