from __future__ import annotations
from dataclasses import dataclass


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

    def retrieve(self, query, changed_node_ids, overlay_ref, changed_paths,
                 top_k=15, candidates=50) -> ContextPack:
        qvec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(
            query_text=query, query_embedding=qvec, overlay_ref=overlay_ref,
            changed_paths=changed_paths, top_k=candidates, candidates=candidates)
        related_ids = self.graph.expand(changed_node_ids, hops=2)
        related = self.store.fetch_nodes(list(related_ids), overlay_ref, changed_paths)
        hit_ids = {h.node_id for h in hits}
        graph_new = [it for it in related if it.node_id not in hit_ids]
        merged: dict[str, object] = {}
        for it in [*hits, *related]:
            merged.setdefault(it.node_id, it)
        # rerank пропускаем, если совсем мало кандидатов, либо все они уже ранжированы RRF
        # (graph-expansion ничего нового не добавил)
        if len(merged) <= 3 or (len(merged) <= top_k and not graph_new):
            return ContextPack(items=list(merged.values()),
                               max_chars=self.max_context_chars)
        ranked = self.reranker.rerank(query, list(merged.values()), top_k=top_k)
        return ContextPack(items=ranked, max_chars=self.max_context_chars)
