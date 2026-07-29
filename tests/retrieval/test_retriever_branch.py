from reviewer.retrieval.retriever import Retriever


class _Hit:
    def __init__(self, nid):
        self.node_id = nid
        self.path = nid.split("#")[0]
        self.symbol_fqn = "f"
        self.kind = "function"
        self.start_line = 1
        self.end_line = 2
        self.text = "code"
        self.score = 1.0
        # реалистичный хит hybrid_search: всегда из bm25- или ann-CTE (PRI-202 ANN-префильтр)
        self.bm25_hit = True
        self.ann_distance = 0.1


class FakeStore:
    def __init__(self):
        self.calls = []

    def hybrid_search(self, repo, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k=20, candidates=50, *, base_ref="base"):
        self.calls.append(base_ref)
        return [_Hit("a.py#f")]

    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths, *, base_ref="base"):
        return []


class FakeGraph:
    def __init__(self):
        self.branches = []

    def expand(self, repo, node_ids, hops=2, *, branch=""):
        self.branches.append(branch)
        return set()

    def expand_detailed(self, repo, node_ids, hops=2, *, branch=""):
        self.branches.append(branch)
        return []


class FakeEmb:
    def embed_query(self, q):
        return [0.0] * 4


def test_retrieve_passes_base_ref_and_branch():
    store, graph = FakeStore(), FakeGraph()
    r = Retriever(store, graph, FakeEmb(), reranker=None)
    r.retrieve("a/x", "q", ["a.py#f"], overlay_ref="pr:1",
               changed_paths=["a.py"], branch="master")
    assert store.calls == ["base:master"]
    assert graph.branches == ["master"]


def test_search_base_passes_branch():
    store, graph = FakeStore(), FakeGraph()
    r = Retriever(store, graph, FakeEmb(), reranker=None)
    r.search_base("a/x", "q", branch="main")
    assert store.calls[0] == "base:main"
    # search_base сидит граф от топ-хитов: hits непусты → expand вызван с веткой
    assert graph.branches == ["main"]
