import pytest

from reviewer.policy.context_limits import CodebaseLimits
from reviewer.retrieval.retriever import ContextPack, Retriever


class _Hit:
    def __init__(self, node_id, score=1.0, start_line=1, end_line=2):
        self.node_id = node_id
        self.path, self.symbol_fqn = node_id.split("#", 1)
        self.kind = "function"
        self.start_line = start_line
        self.end_line = end_line
        self.text = "body"
        self.score = score
        self.ann_distance = None
        self.bm25_hit = False


class _FakeStore:
    def __init__(self, hits, related=None):
        self._hits = hits
        self._related = related or []
        self.search_calls = []
        self.fetch_calls = []

    def hybrid_search(self, repo, *, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k, candidates, base_ref="base"):
        self.search_calls.append({
            "repo": repo, "overlay_ref": overlay_ref, "changed_paths": changed_paths,
            "top_k": top_k, "candidates": candidates,
        })
        return self._hits

    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths, *, base_ref="base"):
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
    def __init__(self, related=(), raise_=False):
        self._related = list(related)
        self.expand_calls = []
        self._raise = raise_

    def expand_detailed(self, repo, node_ids, hops=2, *, branch=""):
        self.expand_calls.append({"repo": repo, "seeds": list(node_ids), "hops": hops,
                                  "branch": branch})
        if self._raise:
            raise RuntimeError("neo4j down")
        return list(self._related)


class _FakeReranker:
    """Возвращает (item, score) по заранее заданным скорам в порядке входа."""
    def __init__(self, scores=None, raise_=False):
        self._scores = scores
        self.calls = []
        self._raise = raise_

    def rerank_scored(self, query, items):
        self.calls.append({"n": len(items)})
        if self._raise:
            raise RuntimeError("voyage down")
        items = list(items)
        scores = self._scores or [1.0 - i * 0.01 for i in range(len(items))]
        paired = list(zip(items, scores[:len(items)]))
        return sorted(paired, key=lambda p: p[1], reverse=True)


def _cb(**kw):
    return CodebaseLimits(**{**dict(floor=1, ceiling=15, ratio=0.5,
                                    abs_floor=0.3, candidate_pool=30, ann_distance_max=0.65), **kw})


def _meta(node_id, dist=1, rels=None):
    return {"id": node_id, "dist": dist, "rels": rels or ["CALLS"]}


@pytest.mark.parametrize(
    ("hybrid_ids", "graph_ids", "ceiling", "expected"),
    [
        (["h1", "h2", "h3"], ["g1", "g2"], 3, ["h1", "h2", "g1"]),
        (["h1"], ["g1", "g2"], 3, ["h1", "g1", "g2"]),
        (["h1", "h2"], ["g1"], 1, ["h1"]),
        ([], ["g1", "g2"], 1, ["g1"]),
        (["h1", "h2"], [], 2, ["h1", "h2"]),
        (["h1"], ["g1"], 0, []),
    ],
)
def test_select_degraded_context(hybrid_ids, graph_ids, ceiling, expected):
    try:
        from reviewer.retrieval.retriever import _select_degraded_context
    except ImportError:
        pytest.fail("_select_degraded_context ещё не реализован")
    hybrid = [_Hit(f"{node_id}.py#f") for node_id in hybrid_ids]
    graph = [_Hit(f"{node_id}.py#f") for node_id in graph_ids]

    selected = _select_degraded_context(hybrid, graph, ceiling)

    assert [item.path.removesuffix(".py") for item in selected] == expected


def test_search_base_is_base_only_and_seeds_graph_from_hits():
    hits = [_Hit("a.py#f1"), _Hit("b.py#f2"), _Hit("c.py#f3"), _Hit("d.py#f4")]
    for h in hits:
        h.bm25_hit = True
    related = [_Hit("e.py#neighbor")]
    store, graph, reranker, emb = (_FakeStore(hits, related),
                                    _FakeGraph([{"id": "e.py#neighbor", "rels": ["CALLS"],
                                                 "dist": 1}]),
                                    _FakeReranker(), _FakeEmbedder())
    r = Retriever(store, graph, emb, reranker, max_context_chars=8000)
    pack = r.search_base("a/x", "logout", limits=_cb())
    assert isinstance(pack, ContextPack)
    assert store.search_calls[0]["changed_paths"] == []
    assert store.search_calls[0]["overlay_ref"] != "base"
    assert emb.queries == ["logout"]
    assert graph.expand_calls and graph.expand_calls[0]["seeds"][0] == "a.py#f1"
    assert store.fetch_calls[0]["changed_paths"] == []
    assert reranker.calls
    assert "a.py#f1" in pack.as_context()


def test_search_base_graph_down_falls_back_to_hybrid():
    hits = [_Hit("a.py#f1")]
    hits[0].bm25_hit = True
    store = _FakeStore(hits)
    r = Retriever(store, _FakeGraph(raise_=True), _FakeEmbedder(), _FakeReranker(),
                  max_context_chars=8000)
    assert "a.py#f1" in r.search_base("a/x", "x", limits=_cb()).as_context()


def test_search_base_empty_returns_empty_pack():
    r = Retriever(_FakeStore([]), graph=None, embedder=_FakeEmbedder(), reranker=None)
    assert r.search_base("a/x", "nothing", limits=_cb()).as_context() == ""


def test_search_base_dedupes_nested_chunks():
    hits = [_Hit("a.py#Foo", start_line=1, end_line=50),
            _Hit("a.py#Foo.bar", start_line=10, end_line=20)]
    for h in hits:
        h.bm25_hit = True
    r = Retriever(_FakeStore(hits), graph=None, embedder=_FakeEmbedder(), reranker=None)
    pack = r.search_base("a/x", "q", limits=_cb())
    assert [it.node_id for it in pack.items] == ["a.py#Foo"]


def test_search_base_filters_tests_by_default():
    hits = [_Hit("a.py#f"), _Hit("tests/test_a.py#t")]
    for h in hits:
        h.bm25_hit = True
    r = Retriever(_FakeStore(hits), graph=None, embedder=_FakeEmbedder(), reranker=None)
    pack = r.search_base("a/x", "q", limits=_cb())
    assert [it.node_id for it in pack.items] == ["a.py#f"]


def test_search_base_include_tests_keeps_tests():
    hits = [_Hit("a.py#f"), _Hit("tests/test_a.py#t")]
    for h in hits:
        h.bm25_hit = True
    r = Retriever(_FakeStore(hits), graph=None, embedder=_FakeEmbedder(), reranker=None)
    pack = r.search_base("a/x", "q", limits=_cb(), include_tests=True)
    assert {it.node_id for it in pack.items} == {"a.py#f", "tests/test_a.py#t"}


def test_search_base_reranks_always_and_applies_cliff():
    hits = [_Hit("a.py#f1", score=0.9), _Hit("b.py#f2"), _Hit("c.py#f3")]
    for h in hits:                       # bm25-хиты → префильтр их не трогает
        h.bm25_hit = True
        h.ann_distance = 0.1
    store, graph = _FakeStore(hits), _FakeGraph()
    reranker = _FakeReranker(scores=[0.91, 0.34, 0.12])   # обрыв после 1-го
    r = Retriever(store, graph, _FakeEmbedder(), reranker, max_context_chars=8000)
    pack = r.search_base("a/x", "x", limits=_cb(floor=2))
    assert reranker.calls                # реранк вызван всегда
    assert [it.node_id for it in pack.items] == ["a.py#f1", "b.py#f2"]  # floor=2


def test_search_base_ann_prefilter_drops_far_non_bm25():
    keep = _Hit("a.py#keep", score=0.9)
    keep.bm25_hit = False
    keep.ann_distance = 0.2
    bm = _Hit("b.py#bm", score=0.5)      # плохой вектор, но лексика
    bm.bm25_hit = True
    bm.ann_distance = 0.95
    drop = _Hit("c.py#drop", score=0.4)
    drop.bm25_hit = False
    drop.ann_distance = 0.95
    store = _FakeStore([keep, bm, drop])
    reranker = _FakeReranker(scores=[0.9, 0.8])
    r = Retriever(store, _FakeGraph(), _FakeEmbedder(), reranker)
    pack = r.search_base("a/x", "x", limits=_cb(floor=1, ann_distance_max=0.65))
    ids = {it.node_id for it in pack.items}
    assert "c.py#drop" not in ids        # далёкий не-BM25 отброшен ДО реранка
    assert reranker.calls[0]["n"] == 2   # реранкнули только 2, не 3


def test_search_base_no_reranker_returns_rrf_order():
    hits = [_Hit("a.py#f1"), _Hit("b.py#f2")]
    for h in hits:                       # реалистичные хиты hybrid_search — всегда из bm25 или ann CTE
        h.bm25_hit = True
    store = _FakeStore(hits)
    r = Retriever(store, graph=None, embedder=_FakeEmbedder(), reranker=None)
    pack = r.search_base("a/x", "x", limits=_cb(ceiling=5))
    assert [it.node_id for it in pack.items] == ["a.py#f1", "b.py#f2"]


def test_search_base_reranker_failure_falls_back_to_rrf():
    hits = [_Hit("a.py#f1"), _Hit("b.py#f2"), _Hit("c.py#f3"), _Hit("d.py#f4")]
    for h in hits:
        h.bm25_hit = True
    store = _FakeStore(hits)
    r = Retriever(store, _FakeGraph(), _FakeEmbedder(), _FakeReranker(raise_=True))
    pack = r.search_base("a/x", "x", limits=_cb(ceiling=2))
    assert len(pack.items) == 2          # RRF-порядок, срез по ceiling
    assert pack.tail_meta is None        # заметка не пишется при фолбэке


def test_search_base_marks_unconfigured_reranker():
    hits = [_Hit(f"h{i}.py#f") for i in range(1, 4)]
    for hit in hits:
        hit.bm25_hit = True
    retriever = Retriever(
        _FakeStore(hits),
        _FakeGraph(),
        _FakeEmbedder(),
        reranker=None,
    )

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=2))

    assert pack.degraded_reason == "reranker_unconfigured"


def test_search_base_marks_failed_reranker():
    hits = [_Hit(f"h{i}.py#f") for i in range(1, 4)]
    for hit in hits:
        hit.bm25_hit = True
    retriever = Retriever(
        _FakeStore(hits),
        _FakeGraph(),
        _FakeEmbedder(),
        _FakeReranker(raise_=True),
    )

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=2))

    assert pack.degraded_reason == "reranker_failed"


def test_search_base_small_pool_is_not_marked_degraded():
    hits = [_Hit("h1.py#f"), _Hit("h2.py#f")]
    for hit in hits:
        hit.bm25_hit = True
    reranker = _FakeReranker()
    retriever = Retriever(_FakeStore(hits), _FakeGraph(), _FakeEmbedder(), reranker)

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=4, ceiling=1))

    assert pack.degraded_reason is None
    assert reranker.calls == []


def test_search_base_successful_rerank_is_not_marked_degraded():
    hits = [_Hit(f"h{i}.py#f") for i in range(1, 4)]
    for hit in hits:
        hit.bm25_hit = True
    retriever = Retriever(
        _FakeStore(hits),
        _FakeGraph(),
        _FakeEmbedder(),
        _FakeReranker(scores=[0.9, 0.8, 0.7]),
    )

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=3))

    assert pack.degraded_reason is None


def test_search_base_seeds_graph_with_configured_hops():
    hits = [_Hit("a.py#f1")]
    hits[0].bm25_hit = True
    graph = _FakeGraph([{"id": "e.py#n", "rels": ["CALLS"], "dist": 1}])
    store = _FakeStore(hits, related=[_Hit("e.py#n")])
    r = Retriever(store, graph, _FakeEmbedder(), _FakeReranker())
    r.search_base("a/x", "x", limits=_cb(), hops=2)
    assert graph.expand_calls[0]["hops"] == 2


def test_search_base_restores_graph_order_after_fetch_nodes():
    hits = [_Hit("h1.py#f")]
    hits[0].bm25_hit = True
    graph = _FakeGraph([_meta("g1.py#f", dist=1), _meta("g2.py#f", dist=2)])
    store = _FakeStore(
        hits,
        related=[_Hit("g2.py#f"), _Hit("g1.py#f")],
    )
    retriever = Retriever(store, graph, _FakeEmbedder(), reranker=None)

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=3))

    assert [item.node_id for item in pack.items] == [
        "h1.py#f", "g1.py#f", "g2.py#f",
    ]


def test_search_base_no_reranker_reserves_best_graph_item():
    hits = [_Hit(f"h{i}.py#f") for i in range(1, 4)]
    for hit in hits:
        hit.bm25_hit = True
    graph = _FakeGraph([_meta("g1.py#f"), _meta("g2.py#f", dist=2)])
    store = _FakeStore(hits, related=[_Hit("g2.py#f"), _Hit("g1.py#f")])
    retriever = Retriever(store, graph, _FakeEmbedder(), reranker=None)

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=3))

    assert [item.node_id for item in pack.items] == [
        "h1.py#f", "h2.py#f", "g1.py#f",
    ]


def test_search_base_reranker_failure_uses_same_graph_reservation():
    hits = [_Hit(f"h{i}.py#f") for i in range(1, 4)]
    for hit in hits:
        hit.bm25_hit = True
    graph = _FakeGraph([_meta("g1.py#f")])
    store = _FakeStore(hits, related=[_Hit("g1.py#f")])
    reranker = _FakeReranker(raise_=True)
    retriever = Retriever(store, graph, _FakeEmbedder(), reranker)

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=3))

    assert [item.node_id for item in pack.items] == [
        "h1.py#f", "h2.py#f", "g1.py#f",
    ]


def test_search_base_filtered_graph_item_does_not_reserve_slot():
    hits = [_Hit("h1.py#f"), _Hit("h2.py#f")]
    for hit in hits:
        hit.bm25_hit = True
    graph = _FakeGraph([_meta("tests/test_graph.py#t")])
    store = _FakeStore(hits, related=[_Hit("tests/test_graph.py#t")])
    retriever = Retriever(store, graph, _FakeEmbedder(), reranker=None)

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=2))

    assert [item.node_id for item in pack.items] == ["h1.py#f", "h2.py#f"]


def test_search_base_deduped_graph_item_does_not_reserve_slot():
    hits = [
        _Hit("a.py#Foo", start_line=1, end_line=50),
        _Hit("b.py#f"),
    ]
    for hit in hits:
        hit.bm25_hit = True
    graph = _FakeGraph([_meta("a.py#Foo.method")])
    store = _FakeStore(
        hits,
        related=[_Hit("a.py#Foo.method", start_line=10, end_line=20)],
    )
    retriever = Retriever(store, graph, _FakeEmbedder(), reranker=None)

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=2))

    assert [item.node_id for item in pack.items] == ["a.py#Foo", "b.py#f"]
