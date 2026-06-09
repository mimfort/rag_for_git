from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ContextPack:
    items: list

    def as_context(self) -> str:
        parts = []
        for it in self.items:
            parts.append(f"// {it.node_id} ({it.path}:{it.start_line}-{it.end_line})\n{it.text}")
        return "\n\n".join(parts)


class Retriever:
    def __init__(self, store, graph, embedder, reranker):
        self.store, self.graph = store, graph
        self.embedder, self.reranker = embedder, reranker

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
            return ContextPack(items=list(merged.values()))
        ranked = self.reranker.rerank(query, list(merged.values()), top_k=top_k)
        return ContextPack(items=ranked)
