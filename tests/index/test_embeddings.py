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
