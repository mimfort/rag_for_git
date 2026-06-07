from __future__ import annotations

from reviewer.index._retry import with_voyage_retry


class VoyageEmbedder:
    def __init__(self, client=None, model: str = "voyage-code-3",
                 dim: int = 1024, batch_size: int = 128):
        if client is None:
            import voyageai
            client = voyageai.Client()      # VOYAGE_API_KEY из env
        self._client = client
        self.model = model
        self.dim = dim
        self.batch_size = batch_size

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            resp = with_voyage_retry(lambda b=batch: self._client.embed(
                b, model=self.model,
                input_type=input_type, output_dimension=self.dim,
            ))
            out.extend(resp.embeddings)
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]
