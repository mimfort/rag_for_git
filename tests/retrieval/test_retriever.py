from reviewer.retrieval.retriever import Retriever
from reviewer.index.store import Retrieved

def R(nid, text):
    p, f = nid.split("#")
    return Retrieved(nid, p, f, "function", 1, 2, text, 1.0)

class FakeStore:
    def hybrid_search(self, **kw): return [R("a.py#f", "alpha"), R("b.py#g", "beta")]
    def fetch_nodes(self, node_ids, overlay_ref, changed_paths):
        return [R("c.py#h", "gamma")] if "c.py#h" in node_ids else []
class FakeGraph:
    def expand(self, ids, hops=2): return {"c.py#h"}
class FakeEmb:
    def embed_query(self, q): return [0.0, 0.1]

class CountingReranker:
    """Reranker, который считает количество вызовов для проверки в тестах."""
    def __init__(self): self.calls = 0
    def rerank(self, query, items, top_k):
        self.calls += 1
        return list(reversed(items))[:top_k]

def test_retrieve_merges_hybrid_and_graph_then_reranks():
    reranker = CountingReranker()
    r = Retriever(FakeStore(), FakeGraph(), FakeEmb(), reranker)
    pack = r.retrieve(query="find f", changed_node_ids=["a.py#f"],
                      overlay_ref="pr:1", changed_paths=["a.py"], top_k=3)
    ids = [x.node_id for x in pack.items]
    assert "c.py#h" in ids
    assert set(ids) == {"a.py#f", "b.py#g", "c.py#h"}
    assert "c.py#h" in pack.as_context()

def test_retrieve_skips_rerank_when_candidates_le_top_k():
    """Если кандидатов не больше top_k, rerank не вызывается — экономим вызов Voyage."""
    reranker = CountingReranker()
    r = Retriever(FakeStore(), FakeGraph(), FakeEmb(), reranker)
    # FakeStore возвращает 2 хита + 1 graph-related = 3 кандидата; top_k=3 → rerank пропускаем
    pack = r.retrieve(query="find f", changed_node_ids=["a.py#f"],
                      overlay_ref="pr:1", changed_paths=["a.py"], top_k=3)
    assert reranker.calls == 0
    assert set(x.node_id for x in pack.items) == {"a.py#f", "b.py#g", "c.py#h"}

def test_retrieve_calls_rerank_when_candidates_gt_top_k():
    """Если кандидатов больше top_k, rerank вызывается как обычно."""
    reranker = CountingReranker()
    r = Retriever(FakeStore(), FakeGraph(), FakeEmb(), reranker)
    # top_k=2 < 3 кандидатов → rerank должен вызваться
    pack = r.retrieve(query="find f", changed_node_ids=["a.py#f"],
                      overlay_ref="pr:1", changed_paths=["a.py"], top_k=2)
    assert reranker.calls == 1
    assert len(pack.items) == 2
