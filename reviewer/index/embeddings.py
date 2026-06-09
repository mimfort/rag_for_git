from __future__ import annotations

from reviewer.index._retry import with_voyage_retry

# Максимальный размер кэша query-эмбеддингов.
# Агент в tool-loop повторяет одинаковые запросы; кэш экономит вызовы Voyage (free tier: 3 RPM).
_DEFAULT_CACHE_SIZE = 512


class VoyageEmbedder:
    def __init__(self, client=None, model: str = "voyage-code-3",
                 dim: int = 1024, batch_size: int = 128,
                 cache_size: int = _DEFAULT_CACHE_SIZE):
        if client is None:
            import voyageai
            client = voyageai.Client()      # VOYAGE_API_KEY из env
        self._client = client
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        # Кэш query-эмбеддингов: text -> vector. FIFO-вытеснение при переполнении.
        # Документы не кэшируем — там дедуп по content_hash уже есть на уровне store.
        self._query_cache: dict[str, list[float]] = {}
        self._cache_size = cache_size

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
        """Вернуть эмбеддинг запроса, используя кэш для повторных обращений."""
        if text in self._query_cache:
            return self._query_cache[text]
        vec = self._embed([text], "query")[0]
        # FIFO-вытеснение: удаляем самую старую запись при переполнении.
        # dict в Python 3.7+ хранит порядок вставки, поэтому первый ключ — самый старый.
        if len(self._query_cache) >= self._cache_size:
            oldest = next(iter(self._query_cache))
            del self._query_cache[oldest]
        self._query_cache[text] = vec
        return vec
