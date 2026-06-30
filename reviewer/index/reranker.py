from __future__ import annotations

from reviewer.index._retry import with_voyage_retry


class VoyageReranker:
    def __init__(self, client=None, model: str = "rerank-2.5"):
        if client is None:
            import voyageai
            client = voyageai.Client()
        self._client = client
        self.model = model

    def rerank_scored(self, query: str, items: list) -> list:
        """Реранк всего пула с сохранением скоров: list[(item, relevance_score)] по убыванию."""
        if not items:
            return []
        docs = [it.text for it in items]
        resp = with_voyage_retry(
            lambda: self._client.rerank(query, docs, model=self.model, top_k=len(docs)))
        return [(items[res.index], float(res.relevance_score)) for res in resp.results]

    def rerank(self, query: str, items: list, top_k: int) -> list:
        """Старая семантика: переупорядоченные items без скоров, усечённые до top_k."""
        return [it for it, _ in self.rerank_scored(query, items)][:top_k]
