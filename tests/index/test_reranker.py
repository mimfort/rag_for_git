from reviewer.index.reranker import VoyageReranker


class _Res:
    def __init__(self, index, score):
        self.index = index
        self.relevance_score = score


class _Resp:
    def __init__(self, results):
        self.results = results


class _FakeClient:
    def __init__(self, results):
        self._results = results
        self.calls = []

    def rerank(self, query, docs, model, top_k):
        self.calls.append({"query": query, "n": len(docs), "top_k": top_k})
        return _Resp(self._results)


class _It:
    def __init__(self, text):
        self.text = text


def test_rerank_scored_returns_item_score_pairs_in_order():
    items = [_It("a"), _It("b"), _It("c")]
    client = _FakeClient([_Res(2, 0.9), _Res(0, 0.4)])     # реранкер вернул c, a
    rr = VoyageReranker(client=client)
    out = rr.rerank_scored("q", items)
    assert [(it.text, sc) for it, sc in out] == [("c", 0.9), ("a", 0.4)]
    assert client.calls[0]["top_k"] == 3                    # реранкаем весь пул


def test_rerank_keeps_items_only_signature():
    items = [_It("a"), _It("b")]
    client = _FakeClient([_Res(1, 0.9), _Res(0, 0.4)])
    rr = VoyageReranker(client=client)
    out = rr.rerank("q", items, top_k=2)
    assert [it.text for it in out] == ["b", "a"]             # без скоров


def test_rerank_scored_empty():
    rr = VoyageReranker(client=_FakeClient([]))
    assert rr.rerank_scored("q", []) == []
