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
