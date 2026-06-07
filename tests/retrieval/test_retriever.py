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
class FakeRerank:
    def rerank(self, query, items, top_k):
        return list(reversed(items))[:top_k]

def test_retrieve_merges_hybrid_and_graph_then_reranks():
    r = Retriever(FakeStore(), FakeGraph(), FakeEmb(), FakeRerank())
    pack = r.retrieve(query="find f", changed_node_ids=["a.py#f"],
                      overlay_ref="pr:1", changed_paths=["a.py"], top_k=3)
    ids = [x.node_id for x in pack.items]
    assert "c.py#h" in ids
    assert set(ids) == {"a.py#f", "b.py#g", "c.py#h"}
    assert "c.py#h" in pack.as_context()
