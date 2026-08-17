import threading

import pytest

from reviewer.index.embeddings import VoyageEmbedder


class FakeResp:
    def __init__(self, embs): self.embeddings = embs

class FakeClient:
    def __init__(self): self.calls = []
    def embed(self, texts, model, input_type, output_dimension):
        self.calls.append((tuple(texts), input_type))
        return FakeResp([[0.1] * output_dimension for _ in texts])

def test_embed_documents_batches_and_uses_document_input_type():
    fake = FakeClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=1024, batch_size=2)
    vecs = emb.embed_documents(["a", "b", "c"])
    assert len(vecs) == 3 and len(vecs[0]) == 1024
    assert [c[1] for c in fake.calls] == ["document", "document"]   # 2 батча
    assert fake.calls[0][0] == ("a", "b") and fake.calls[1][0] == ("c",)

def test_embed_query_uses_query_input_type():
    fake = FakeClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=8)
    v = emb.embed_query("find me")
    assert len(v) == 8 and fake.calls[0][1] == "query"

def test_embed_query_cache_deduplicates_identical_texts():
    """Два вызова embed_query с одним текстом → один вызов клиента (кэш)."""
    fake = FakeClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=8)
    v1 = emb.embed_query("токены")
    v2 = emb.embed_query("токены")
    assert v1 == v2
    assert len(fake.calls) == 1   # второй вызов взят из кэша

def test_embed_query_cache_separates_different_texts():
    """Разные тексты → разные вызовы клиента."""
    fake = FakeClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=8)
    emb.embed_query("alpha")
    emb.embed_query("beta")
    assert len(fake.calls) == 2

def test_embed_query_cache_fifo_eviction():
    """При переполнении кэша самая старая запись вытесняется (FIFO)."""
    fake = FakeClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=8, cache_size=2)
    emb.embed_query("first")    # кэш: {first}
    emb.embed_query("second")   # кэш: {first, second}
    emb.embed_query("third")    # кэш: {second, third}; "first" вытеснен
    calls_before = len(fake.calls)
    emb.embed_query("second")   # "second" всё ещё в кэше → без вызова
    assert len(fake.calls) == calls_before
    emb.embed_query("first")    # "first" уже не в кэше → новый вызов
    assert len(fake.calls) == calls_before + 1


def test_embed_query_cache_is_thread_safe_for_same_text():
    """Параллельные вызовы embed_query с одинаковым текстом порождают ровно один вызов клиента."""
    fake = FakeClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=8, cache_size=4)

    results: list[list[float]] = []
    lock = threading.Lock()

    def call() -> None:
        vec = emb.embed_query("shared query")
        with lock:
            results.append(vec)

    threads = [threading.Thread(target=call) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(fake.calls) == 1
    assert len(results) == 10
    assert all(v == results[0] for v in results)


class CountingClient(FakeClient):
    """Клиент со счётчиком токенов Voyage: 1 токен на символ (удобно для порогов)."""

    def count_tokens(self, texts, model):
        return sum(len(t) for t in texts)


class TokenLimitClient(CountingClient):
    """Отвергает батч дороже hard_limit — как валидационный отказ Voyage."""

    def __init__(self, hard_limit: int):
        super().__init__()
        self.hard_limit = hard_limit
        self.rejected: list[int] = []

    def embed(self, texts, model, input_type, output_dimension):
        tokens = sum(len(t) for t in texts)
        if tokens > self.hard_limit:
            self.rejected.append(tokens)
            raise ValueError(
                f"The max allowed tokens per submitted batch is {self.hard_limit}. "
                f"Your batch has {tokens} tokens after truncation."
            )
        return super().embed(texts, model, input_type, output_dimension)


def test_batches_are_capped_by_token_budget_not_by_count():
    """Суммарная оценка выше бюджета → несколько вызовов, каждый в пределах бюджета."""
    fake = CountingClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=8,
                         batch_size=256, token_budget=100)
    texts = ["x" * 40 for _ in range(5)]      # 200 токенов суммарно при бюджете 100
    vecs = emb.embed_documents(texts)
    assert len(vecs) == 5
    assert len(fake.calls) > 1
    for batch, _ in fake.calls:
        assert sum(len(t) for t in batch) <= 100


def test_batch_size_still_caps_element_count():
    """Дешёвые чанки не собираются в батч больше embedding_batch_size."""
    fake = CountingClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=8,
                         batch_size=2, token_budget=100_000)
    emb.embed_documents(["a", "b", "c", "d", "e"])
    assert [len(batch) for batch, _ in fake.calls] == [2, 2, 1]


def test_oversized_single_chunk_is_truncated_not_fatal():
    """Одиночный чанк дороже бюджета усекается, индексация продолжается."""
    fake = CountingClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=8,
                         batch_size=256, token_budget=50)
    vecs = emb.embed_documents(["y" * 500, "ok"])
    assert len(vecs) == 2
    for batch, _ in fake.calls:
        assert sum(len(t) for t in batch) <= 50


def test_underestimated_batch_is_split_and_retried():
    """Недооценка токенов → батч делится пополам и повторяется, а не падает."""
    fake = TokenLimitClient(hard_limit=60)
    # Бюджет намеренно выше hard_limit: имитирует недооценку токенов.
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=8,
                         batch_size=256, token_budget=200)
    vecs = emb.embed_documents(["z" * 50, "z" * 50])
    assert len(vecs) == 2
    assert fake.rejected == [100]              # ровно один отказ, затем деление
    assert [len(batch) for batch, _ in fake.calls] == [1, 1]


def test_non_token_errors_are_not_swallowed():
    """Прочие ошибки клиента пробрасываются — деление батча их не маскирует."""

    class BrokenClient(CountingClient):
        def embed(self, texts, model, input_type, output_dimension):
            raise ValueError("upstream is down")

    emb = VoyageEmbedder(client=BrokenClient(), model="voyage-code-3", dim=8)
    with pytest.raises(ValueError, match="upstream is down"):
        emb.embed_documents(["a"])


def test_embed_queries_batches_misses_and_reuses_cache():
    """Один сетевой вызов на все промахи; попадания кэша не эмбеддятся заново."""
    fake = FakeClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=8)
    emb.embed_query("первый")
    calls_before = len(fake.calls)
    vectors = emb.embed_queries(["первый", "второй", "третий", "второй"])
    assert len(vectors) == 4
    assert vectors[1] == vectors[3]                      # повтор отдаёт тот же вектор
    assert len(fake.calls) == calls_before + 1           # промахи ушли одним батчем
    assert fake.calls[-1][0] == ("второй", "третий")     # в батче только промахи, без дублей


def test_embed_queries_uses_query_input_type():
    fake = FakeClient()
    emb = VoyageEmbedder(client=fake, model="voyage-code-3", dim=8)
    emb.embed_queries(["запрос"])
    assert fake.calls[-1][1] == "query"
