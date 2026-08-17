"""RRF-слияние выдач подзапросов и обрезка блока рендера (PRI-255)."""
from reviewer.index.store import Retrieved
from reviewer.policy.context_limits import CodebaseLimits
from reviewer.retrieval.multiquery import MAX_BLOCK_CHARS, cap_block, rrf_merge, search_multi


def _hit(node_id: str, start_line: int = 1, end_line: int = 2, text: str = "body"):
    path, fqn = node_id.split("#", 1)
    return Retrieved(node_id=node_id, path=path, symbol_fqn=fqn, kind="function",
                     start_line=start_line, end_line=end_line, text=text, score=1.0)


def test_found_by_two_subqueries_outranks_leader_of_one():
    # a.py#f второй в обоих прогонах, b.py#g первый в одном: сумма 1/62+1/62 > 1/61
    merged = rrf_merge([
        [_hit("b.py#g"), _hit("a.py#f")],
        [_hit("c.py#h"), _hit("a.py#f")],
    ])
    assert [it.node_id for it in merged][0] == "a.py#f"


def test_single_subquery_hit_still_present():
    merged = rrf_merge([[_hit("a.py#f")], [_hit("b.py#g")]])
    assert {it.node_id for it in merged} == {"a.py#f", "b.py#g"}


def test_score_is_rrf_sum():
    merged = rrf_merge([[_hit("a.py#f")], [_hit("a.py#f")]])
    assert merged[0].score == 2 * (1.0 / 61)


def test_ties_broken_deterministically_by_node_id():
    first = rrf_merge([[_hit("b.py#g")], [_hit("a.py#f")]])
    second = rrf_merge([[_hit("a.py#f")], [_hit("b.py#g")]])
    assert [it.node_id for it in first] == [it.node_id for it in second]


def test_empty_runs_yield_empty_result():
    assert rrf_merge([]) == []
    assert rrf_merge([[], []]) == []


def test_short_block_is_untouched():
    item = _hit("a.py#f", 10, 11, "one\ntwo")
    assert cap_block(item) is item


def test_long_block_is_cut_on_line_boundary_with_honest_end_line():
    body = "\n".join(f"строка {i}" * 20 for i in range(200))
    item = _hit("a.py#f", start_line=100, end_line=299, text=body)
    capped = cap_block(item)
    assert len(capped.text) <= MAX_BLOCK_CHARS
    assert capped.text.splitlines() == item.text.splitlines()[: len(capped.text.splitlines())]
    assert capped.end_line == 100 + len(capped.text.splitlines()) - 1
    assert capped.end_line < 299


def test_cap_block_does_not_mutate_source():
    item = _hit("a.py#f", 1, 400, "x" * 5000)
    cap_block(item)
    assert len(item.text) == 5000


class _FakeEmbedder:
    def __init__(self, fail_batch: bool = False):
        self.batches: list = []
        self.singles: list = []
        self._fail_batch = fail_batch

    def embed_queries(self, texts):
        if self._fail_batch:
            raise RuntimeError("нет квоты")
        self.batches.append(list(texts))
        return [[0.1] * 8 for _ in texts]

    def embed_query(self, text):
        self.singles.append(text)
        return [0.1] * 8


class _FakeStore:
    def __init__(self, by_query: dict, fail_for: str | None = None):
        self._by_query = by_query
        self._fail_for = fail_for
        self.queries: list = []

    def hybrid_search(self, repo, *, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k, candidates, base_ref="base"):
        self.queries.append(query_text)
        if query_text == self._fail_for:
            raise RuntimeError("сбой прогона")
        return list(self._by_query.get(query_text, []))

    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths, *, base_ref="base"):
        return []


class _Retriever:
    def __init__(self, store, embedder, graph=None, max_context_chars=8000):
        self.store, self.embedder, self.graph = store, embedder, graph
        self.max_context_chars = max_context_chars


def _bm25(node_id: str, text: str = "body"):
    item = _hit(node_id, text=text)
    item.bm25_hit = True
    return item


def test_one_batched_embedding_call_per_assembly():
    embedder = _FakeEmbedder()
    store = _FakeStore({"q0": [_bm25("a.py#f")], "q1": [_bm25("b.py#g")]})
    pack = search_multi(_Retriever(store, embedder), "o/n", ["q0", "q1"],
                        limits=CodebaseLimits(), branch="dev")
    assert embedder.batches == [["q0", "q1"]], "ровно один вызов эмбеддера"
    assert embedder.singles == []
    assert store.queries == ["q0", "q1"], "по прогону на подзапрос"
    assert {it.path for it in pack.items} == {"a.py", "b.py"}


def test_tail_subquery_only_hit_reaches_render():
    """Критерий приёмки 3: файл, найденный только хвостовым подзапросом."""
    store = _FakeStore({
        "q0": [_bm25("core.py#a")],
        "хвостовой пункт": [_bm25("tail.py#z")],
    })
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n",
                        ["q0", "хвостовой пункт"], limits=CodebaseLimits(), branch="dev")
    assert "tail.py" in pack.as_context(line_numbers=True)


def test_failed_batch_falls_back_to_single_query():
    embedder = _FakeEmbedder(fail_batch=True)
    store = _FakeStore({"q0": [_bm25("a.py#f")]})
    pack = search_multi(_Retriever(store, embedder), "o/n", ["q0", "q1"],
                        limits=CodebaseLimits(), branch="dev")
    assert embedder.singles == ["q0"]
    assert store.queries == ["q0"], "откат идёт по первому подзапросу"
    assert pack.items


def test_failed_run_is_skipped_and_others_merge():
    store = _FakeStore({"q0": [_bm25("a.py#f")], "q1": [_bm25("b.py#g")]}, fail_for="q0")
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0", "q1"],
                        limits=CodebaseLimits(), branch="dev")
    assert {it.path for it in pack.items} == {"b.py"}


def test_tests_are_filtered_unless_requested():
    store = _FakeStore({"q0": [_bm25("tests/test_a.py#t"), _bm25("a.py#f")]})
    retriever = _Retriever(store, _FakeEmbedder())
    without = search_multi(retriever, "o/n", ["q0"], limits=CodebaseLimits(), branch="dev")
    assert {it.path for it in without.items} == {"a.py"}
    with_tests = search_multi(retriever, "o/n", ["q0"], limits=CodebaseLimits(),
                              branch="dev", include_tests=True)
    assert "tests/test_a.py" in {it.path for it in with_tests.items}


def test_ann_prefilter_drops_distant_non_lexical_hit():
    far = _hit("far.py#x")
    far.bm25_hit, far.ann_distance = False, 0.99
    store = _FakeStore({"q0": [_bm25("a.py#f"), far]})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    assert {it.path for it in pack.items} == {"a.py"}


def test_ceiling_caps_merged_output():
    hits = [_bm25(f"f{i}.py#s") for i in range(40)]
    store = _FakeStore({"q0": hits})
    limits = CodebaseLimits(ceiling=5)
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=limits, branch="dev")
    assert len(pack.items) == 5


def test_blocks_are_capped_before_render():
    big = _bm25("a.py#f", text="\n".join("x" * 100 for _ in range(200)))
    store = _FakeStore({"q0": [big, _bm25("b.py#g")]})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    context = pack.as_context(line_numbers=True)
    assert "b.py" in context, "второй файл не вытеснен большим блоком"
    assert "[...truncated]" not in context
