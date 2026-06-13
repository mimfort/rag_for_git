from reviewer.retrieval.retriever import ContextPack, Retriever


class _Hit:
    def __init__(self, node_id, score=1.0):
        self.node_id = node_id
        self.path, self.symbol_fqn = node_id.split("#", 1)
        self.kind = "function"
        self.start_line = 1
        self.end_line = 2
        self.text = "body"
        self.score = score


class _FakeStore:
    def __init__(self, hits, related=None):
        self._hits = hits
        self._related = related or []
        self.search_calls = []
        self.fetch_calls = []

    def hybrid_search(self, repo, *, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k, candidates):
        self.search_calls.append({
            "repo": repo, "overlay_ref": overlay_ref, "changed_paths": changed_paths,
            "top_k": top_k, "candidates": candidates,
        })
        return self._hits

    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths):
        self.fetch_calls.append({
            "repo": repo, "node_ids": list(node_ids), "overlay_ref": overlay_ref,
            "changed_paths": changed_paths,
        })
        return self._related


class _FakeEmbedder:
    def __init__(self):
        self.queries = []

    def embed_query(self, text):
        self.queries.append(text)
        return [0.1] * 8


class _FakeGraph:
    def __init__(self, related_ids=(), raise_=False):
        self._ids = set(related_ids)
        self.expand_calls = []
        self._raise = raise_

    def expand(self, repo, node_ids, hops=2):
        self.expand_calls.append({"repo": repo, "seeds": list(node_ids), "hops": hops})
        if self._raise:
            raise RuntimeError("neo4j down")
        return set(self._ids)


class _FakeReranker:
    def __init__(self):
        self.calls = []

    def rerank(self, query, items, top_k):
        self.calls.append({"n": len(items), "top_k": top_k})
        return list(items)[:top_k]


def test_search_base_is_base_only_and_seeds_graph_from_hits():
    hits = [_Hit("a.py#f1"), _Hit("b.py#f2"), _Hit("c.py#f3"), _Hit("d.py#f4")]
    related = [_Hit("e.py#neighbor")]
    store, graph, reranker, emb = _FakeStore(hits, related), _FakeGraph({"e.py#neighbor"}), _FakeReranker(), _FakeEmbedder()
    r = Retriever(store, graph, emb, reranker, max_context_chars=8000)
    pack = r.search_base("a/x", "logout", top_k=3)
    assert isinstance(pack, ContextPack)
    assert store.search_calls[0]["changed_paths"] == []
    assert store.search_calls[0]["overlay_ref"] != "base"
    assert emb.queries == ["logout"]
    assert graph.expand_calls and graph.expand_calls[0]["seeds"][0] == "a.py#f1"
    assert store.fetch_calls[0]["changed_paths"] == []
    assert reranker.calls and reranker.calls[0]["top_k"] == 3
    assert "a.py#f1" in pack.as_context()


def test_search_base_graph_down_falls_back_to_hybrid():
    store = _FakeStore([_Hit("a.py#f1")])
    r = Retriever(store, _FakeGraph(raise_=True), _FakeEmbedder(), _FakeReranker(), max_context_chars=8000)
    assert "a.py#f1" in r.search_base("a/x", "x").as_context()


def test_search_base_no_reranker_returns_rrf_order():
    store = _FakeStore([_Hit("a.py#f1"), _Hit("b.py#f2")])
    r = Retriever(store, graph=None, embedder=_FakeEmbedder(), reranker=None)
    pack = r.search_base("a/x", "x", top_k=5)
    assert [it.node_id for it in pack.items] == ["a.py#f1", "b.py#f2"]


def test_search_base_reranks_when_many_hits_and_graph_adds_nothing():
    # реранкер есть, хитов > top_k, граф ничего не добавил (graph_new=False) → реранк всё равно идёт
    hits = [_Hit(f"f{i}.py#fn") for i in range(6)]
    store = _FakeStore(hits)            # related=[] → граф ничего не добавит
    graph = _FakeGraph()                # expand → пустое множество
    reranker = _FakeReranker()
    r = Retriever(store, graph, _FakeEmbedder(), reranker, max_context_chars=8000)
    pack = r.search_base("a/x", "x", top_k=3)
    assert reranker.calls and reranker.calls[0]["top_k"] == 3
    assert len(pack.items) == 3


def test_search_base_empty_returns_empty_pack():
    r = Retriever(_FakeStore([]), graph=None, embedder=_FakeEmbedder(), reranker=None)
    assert r.search_base("a/x", "nothing").as_context() == ""
