from reviewer.retrieval.retriever import ContextPack, Retriever
from reviewer.index.store import Retrieved


def R(nid, text):
    p, f = nid.split("#")
    return Retrieved(nid, p, f, "function", 1, 2, text, 1.0)


class FakeStore:
    def hybrid_search(self, repo, **kw): return [R("a.py#f", "alpha"), R("b.py#g", "beta")]
    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths):
        return [R("c.py#h", "gamma")] if "c.py#h" in node_ids else []


class FakeGraph:
    def expand(self, repo, ids, hops=2): return {"c.py#h"}


class FakeEmb:
    def embed_query(self, q): return [0.0, 0.1]


class CountingReranker:
    """Reranker, который считает количество вызовов для проверки в тестах."""
    def __init__(self): self.calls = 0
    def rerank(self, query, items, top_k):
        self.calls += 1
        return list(reversed(items))[:top_k]


# ---------------------------------------------------------------------------
# Вспомогательные фейки для тестов с разным количеством кандидатов
# ---------------------------------------------------------------------------

class StoreOnlyHits:
    """Только hits, граф ничего нового не добавляет."""
    def hybrid_search(self, repo, **kw): return [R("a.py#f", "alpha"), R("b.py#g", "beta")]
    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths): return []


class GraphEmpty:
    """Граф не расширяет — expand возвращает пустое множество."""
    def expand(self, repo, ids, hops=2): return set()


class StoreManyHits:
    """Много hits, чтобы len(merged) > top_k."""
    def hybrid_search(self, repo, **kw):
        return [R(f"x.py#f{i}", f"text{i}") for i in range(10)]
    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths): return []


class StoreWithGraphNew:
    """Возвращает 3 hits; граф добавляет ещё один — итого 4 кандидата (> 3)."""
    def hybrid_search(self, repo, **kw):
        return [R("a.py#f", "alpha"), R("b.py#g", "beta"), R("c.py#h", "gamma")]
    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths):
        return [R("d.py#i", "delta")] if "d.py#i" in node_ids else []


class GraphNewOne:
    """Добавляет один узел, которого нет в hits."""
    def expand(self, repo, ids, hops=2): return {"d.py#i"}


# ---------------------------------------------------------------------------
# Тест: базовое слияние hits + graph и rerank при merged > top_k
# ---------------------------------------------------------------------------

def test_retrieve_merges_hybrid_and_graph_then_reranks():
    reranker = CountingReranker()
    r = Retriever(FakeStore(), FakeGraph(), FakeEmb(), reranker)
    pack = r.retrieve("a/x", query="find f", changed_node_ids=["a.py#f"],
                      overlay_ref="pr:1", changed_paths=["a.py"], top_k=3)
    ids = [x.node_id for x in pack.items]
    assert "c.py#h" in ids
    assert set(ids) == {"a.py#f", "b.py#g", "c.py#h"}
    assert "c.py#h" in pack.as_context()


# ---------------------------------------------------------------------------
# Тест 1: только hits, len(merged) <= top_k → rerank НЕ вызывается
# ---------------------------------------------------------------------------

def test_retrieve_skips_rerank_only_hits_le_top_k():
    """Если кандидаты только из hits и их не больше top_k, rerank не вызывается.

    graph_new пустой и len(merged) <= top_k — экономим вызов Voyage rerank.
    """
    reranker = CountingReranker()
    r = Retriever(StoreOnlyHits(), GraphEmpty(), FakeEmb(), reranker)
    # StoreOnlyHits: 2 hits, граф ничего не добавляет; top_k=5 → merged=2 <= top_k, graph_new=[]
    pack = r.retrieve("a/x", query="find f", changed_node_ids=["a.py#f"],
                      overlay_ref="pr:1", changed_paths=["a.py"], top_k=5)
    assert reranker.calls == 0
    assert set(x.node_id for x in pack.items) == {"a.py#f", "b.py#g"}


# ---------------------------------------------------------------------------
# Тест 2: граф добавил новый элемент, итого > 3 → rerank ВЫЗЫВАЕТСЯ
# ---------------------------------------------------------------------------

def test_retrieve_calls_rerank_when_graph_adds_new_and_total_gt_3():
    """Если graph-expansion добавил новый элемент и merged > 3, rerank вызывается.

    Несмотря на то что len(merged) == 4 == top_k, граф принёс новые
    неранжированные элементы — rerank нужен для корректного упорядочивания.
    """
    reranker = CountingReranker()
    r = Retriever(StoreWithGraphNew(), GraphNewOne(), FakeEmb(), reranker)
    # 3 hits + 1 graph_new = 4 merged; top_k=4; len>3 → rerank вызывается
    pack = r.retrieve("a/x", query="find f", changed_node_ids=["a.py#f"],
                      overlay_ref="pr:1", changed_paths=["a.py"], top_k=4)
    assert reranker.calls == 1
    assert len(pack.items) == 4


# ---------------------------------------------------------------------------
# Тест 3: граф добавил элементы, но итого <= 3 → rerank НЕ вызывается
# ---------------------------------------------------------------------------

def test_retrieve_skips_rerank_when_total_le_3():
    """Если суммарных кандидатов не больше 3 — rerank не вызывается независимо от graph_new.

    FakeStore: 2 hits; FakeGraph добавляет c.py#h (новый) → merged=3 ≤ 3 → пропуск.
    """
    reranker = CountingReranker()
    r = Retriever(FakeStore(), FakeGraph(), FakeEmb(), reranker)
    # 2 hits + 1 graph_new = 3; top_k=10; len(merged)=3 ≤ 3 → rerank пропускаем
    pack = r.retrieve("a/x", query="find f", changed_node_ids=["a.py#f"],
                      overlay_ref="pr:1", changed_paths=["a.py"], top_k=10)
    assert reranker.calls == 0
    assert set(x.node_id for x in pack.items) == {"a.py#f", "b.py#g", "c.py#h"}


# ---------------------------------------------------------------------------
# Тест 4: len(merged) > top_k → rerank вызывается (как раньше)
# ---------------------------------------------------------------------------

def test_retrieve_calls_rerank_when_candidates_gt_top_k():
    """Если кандидатов больше top_k, rerank вызывается как обычно."""
    reranker = CountingReranker()
    r = Retriever(StoreManyHits(), GraphEmpty(), FakeEmb(), reranker)
    # StoreManyHits: 10 hits, top_k=5 → merged=10 > 5 → rerank вызывается
    pack = r.retrieve("a/x", query="find f", changed_node_ids=[],
                      overlay_ref="pr:1", changed_paths=[], top_k=5)
    assert reranker.calls == 1
    assert len(pack.items) == 5


# ---------------------------------------------------------------------------
# Тест: repo прокидывается в store
# ---------------------------------------------------------------------------

class StoreRecordingRepo:
    """Записывает repo, переданный в hybrid_search."""
    def __init__(self):
        self.last_repo = None

    def hybrid_search(self, repo, **kw):
        self.last_repo = repo
        return []

    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths):
        return []


def test_retrieve_threads_repo_to_store():
    store = StoreRecordingRepo()
    r = Retriever(store, GraphEmpty(), FakeEmb(), CountingReranker())
    r.retrieve("a/x", "q", changed_node_ids=[], overlay_ref="pr:1", changed_paths=[])
    assert store.last_repo == "a/x"


# ---------------------------------------------------------------------------
# Сохранённые старые тесты (переименованы для совместимости)
# ---------------------------------------------------------------------------

def test_retrieve_skips_rerank_when_candidates_le_top_k():
    """Обратная совместимость: 3 кандидата (все <= 3), top_k=3 → rerank пропускается."""
    reranker = CountingReranker()
    r = Retriever(FakeStore(), FakeGraph(), FakeEmb(), reranker)
    # FakeStore: 2 hits + 1 graph-related = 3 кандидата; top_k=3 → len(merged)=3 ≤ 3 → пропуск
    pack = r.retrieve("a/x", query="find f", changed_node_ids=["a.py#f"],
                      overlay_ref="pr:1", changed_paths=["a.py"], top_k=3)
    assert reranker.calls == 0
    assert set(x.node_id for x in pack.items) == {"a.py#f", "b.py#g", "c.py#h"}


def test_retrieve_calls_rerank_when_more_candidates_than_top_k():
    """Если кандидатов больше top_k, rerank вызывается."""
    reranker = CountingReranker()
    r = Retriever(StoreManyHits(), GraphEmpty(), FakeEmb(), reranker)
    # StoreManyHits: 10 hits, граф ничего не добавляет; top_k=2 < 10 → rerank вызывается
    pack = r.retrieve("a/x", query="find f", changed_node_ids=[],
                      overlay_ref="pr:1", changed_paths=[], top_k=2)
    assert reranker.calls == 1
    assert len(pack.items) == 2


# ---------------------------------------------------------------------------
# ContextPack truncation
# ---------------------------------------------------------------------------


def test_context_pack_truncates_by_chars():
    """as_context обрезает по max_chars и добавляет маркер."""
    items = [R("a.py#f", "alpha"), R("b.py#g", "beta")]
    pack = ContextPack(items=items, max_chars=20)
    ctx = pack.as_context()
    assert "[...truncated]" in ctx
    assert len(ctx) <= 40


def test_context_pack_truncates_by_tokens():
    """as_context обрезает примерно по max_tokens (1 токен ≈ 4 символа)."""
    items = [R("a.py#f", "alpha"), R("b.py#g", "beta")]
    pack = ContextPack(items=items, max_tokens=2)
    ctx = pack.as_context()
    assert "[...truncated]" in ctx


def test_context_pack_no_truncation_when_unlimited():
    """Без лимитов текст возвращается полностью."""
    items = [R("a.py#f", "alpha"), R("b.py#g", "beta")]
    pack = ContextPack(items=items)
    ctx = pack.as_context()
    assert "alpha" in ctx and "beta" in ctx
    assert "[...truncated]" not in ctx
