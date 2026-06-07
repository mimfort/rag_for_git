from __future__ import annotations

from reviewer.index._retry import with_voyage_retry


class VoyageReranker:
    def __init__(self, client=None, model: str = "rerank-2.5"):
        if client is None:
            import voyageai
            client = voyageai.Client()
        self._client = client
        self.model = model

    def rerank(self, query: str, items: list, top_k: int) -> list:
        if not items:
            return []
        docs = [it.text for it in items]
        resp = with_voyage_retry(
            lambda: self._client.rerank(query, docs, model=self.model, top_k=top_k))
        return [items[res.index] for res in resp.results]
