"""RRF-слияние выдач подзапросов и обрезка блока рендера (PRI-255)."""
from reviewer.index.store import Retrieved
from reviewer.policy.context_limits import CodebaseLimits, CodeSectionLimits
from reviewer.retrieval.multiquery import (
    cap_block, diversify_by_file, rrf_merge, search_multi,
)


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
    assert cap_block(item, 2000) is item


def test_long_block_is_cut_on_line_boundary_with_honest_end_line():
    body = "\n".join(f"строка {i}" * 20 for i in range(200))
    item = _hit("a.py#f", start_line=100, end_line=299, text=body)
    capped = cap_block(item, 2000)
    assert len(capped.text) <= 2000
    assert capped.text.splitlines() == item.text.splitlines()[: len(capped.text.splitlines())]
    assert capped.end_line == 100 + len(capped.text.splitlines()) - 1
    assert capped.end_line < 299


def test_cap_block_does_not_mutate_source():
    item = _hit("a.py#f", 1, 400, "x" * 5000)
    cap_block(item, 2000)
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
    def __init__(self, by_query: dict, fail_for: str | None = None,
                 nodes_by_id: dict | None = None):
        self._by_query = by_query
        self._fail_for = fail_for
        self._nodes_by_id = nodes_by_id or {}
        self.queries: list = []

    def hybrid_search(self, repo, *, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k, candidates, base_ref="base"):
        self.queries.append(query_text)
        if query_text == self._fail_for:
            raise RuntimeError("сбой прогона")
        return list(self._by_query.get(query_text, []))

    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths, *, base_ref="base"):
        return [self._nodes_by_id[node_id] for node_id in node_ids
                if node_id in self._nodes_by_id]


class _FakeGraph:
    def __init__(self, nodes: list[str] | None = None, fail: bool = False):
        self._nodes = nodes or []
        self._fail = fail
        self.calls: list = []

    def expand_detailed(self, repo, seeds, hops, branch):
        self.calls.append(list(seeds))
        if self._fail:
            raise RuntimeError("граф недоступен")
        return [{"id": node_id} for node_id in self._nodes]


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


def test_file_budget_caps_distinct_files():
    """Выдачу режет файловый бюджет секции, а не чанковый ceiling общего поиска."""
    hits = [_bm25(f"f{i}.py#s") for i in range(40)]
    store = _FakeStore({"q0": hits})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(ceiling=5), branch="dev")
    assert len({it.path for it in pack.items}) == 12, "дефолт max_files, а не ceiling=5"


def test_blocks_are_capped_before_render():
    big = _bm25("a.py#f", text="\n".join("x" * 100 for _ in range(200)))
    store = _FakeStore({"q0": [big, _bm25("b.py#g")]})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    context = pack.as_context(line_numbers=True)
    assert "b.py" in context, "второй файл не вытеснен большим блоком"
    assert "[...truncated]" not in context


def test_graph_only_hit_appended_after_hybrid_without_duplicating():
    store = _FakeStore(
        {"q0": [_bm25("a.py#f")]},
        nodes_by_id={"a.py#f": _hit("a.py#f"), "c.py#h": _hit("c.py#h")},
    )
    graph = _FakeGraph(nodes=["a.py#f", "c.py#h"])  # a.py#f уже в hybrid — не задваивается
    pack = search_multi(_Retriever(store, _FakeEmbedder(), graph=graph), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    assert [it.node_id for it in pack.items] == ["a.py#f", "c.py#h"], \
        "graph-only идёт после hybrid, без дублей уже найденного"


def test_graph_expansion_failure_is_fail_soft():
    store = _FakeStore({"q0": [_bm25("a.py#f")]})
    graph = _FakeGraph(fail=True)
    pack = search_multi(_Retriever(store, _FakeEmbedder(), graph=graph), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    assert {it.path for it in pack.items} == {"a.py"}, "сбой графа не роняет сборку"


def test_duplicate_queries_are_deduplicated_preserving_order():
    # "z" > "a" по сортировке: вход намеренно не отсортирован, чтобы пришпилить
    # именно dict.fromkeys-семантику (первое вхождение), а не sorted(set(...)).
    embedder = _FakeEmbedder()
    store = _FakeStore({"z": [_bm25("a.py#f")], "a": [_bm25("b.py#g")]})
    pack = search_multi(_Retriever(store, embedder), "o/n", ["z", "a", "z"],
                        limits=CodebaseLimits(), branch="dev")
    assert embedder.batches == [["z", "a"]], "дубль не уходит в батч эмбеддера повторно"
    assert store.queries == ["z", "a"], "дубль не даёт второй прогон гибрида"
    assert {it.path for it in pack.items} == {"a.py", "b.py"}


def test_diversify_keeps_one_chunk_per_file_by_default():
    items = [_hit("a.py#f1"), _hit("a.py#f2"), _hit("b.py#g")]
    kept = diversify_by_file(items, max_files=10, max_chunks_per_file=1)
    assert [it.node_id for it in kept] == ["a.py#f1", "b.py#g"]


def test_diversify_allows_several_chunks_when_configured():
    items = [_hit("a.py#f1"), _hit("a.py#f2"), _hit("a.py#f3")]
    kept = diversify_by_file(items, max_files=10, max_chunks_per_file=2)
    assert [it.node_id for it in kept] == ["a.py#f1", "a.py#f2"]


def test_diversify_caps_distinct_files_and_keeps_input_order():
    items = [_hit(f"f{i}.py#s") for i in range(10)]
    kept = diversify_by_file(items, max_files=3, max_chunks_per_file=1)
    assert [it.path for it in kept] == ["f0.py", "f1.py", "f2.py"]


def test_diversify_degenerate_values_do_not_crash():
    items = [_hit("a.py#f"), _hit("b.py#g")]
    assert diversify_by_file(items, max_files=0, max_chunks_per_file=1) == []
    assert diversify_by_file([], max_files=5, max_chunks_per_file=1) == []


def test_graph_only_tail_yields_to_hybrid_files():
    """Приоритет входного порядка: hybrid-файлы занимают бюджет раньше graph-only."""
    graph_node = _hit("graph.py#z")
    store = _FakeStore({"q0": [_bm25("hyb.py#a")]},
                       nodes_by_id={"graph.py#z": graph_node})
    retriever = _Retriever(store, _FakeEmbedder(), graph=_FakeGraph(["graph.py#z"]))
    pack = search_multi(retriever, "o/n", ["q0"], limits=CodebaseLimits(),
                        section_limits=CodeSectionLimits(max_files=1), branch="dev")
    assert [it.path for it in pack.items] == ["hyb.py"]


def test_section_budget_fits_selected_files_without_truncation():
    """Производный бюджет обязан вмещать то, что отобрано: среза строки нет."""
    body = "\n".join("y" * 80 for _ in range(60))
    store = _FakeStore({"q0": [_bm25(f"f{i}.py#s", text=body) for i in range(12)]})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    context = pack.as_context(line_numbers=True)
    assert "[...truncated]" not in context
    assert len({it.path for it in pack.items}) == 12


def test_section_budget_ignores_retriever_max_context_chars():
    """Бюджет секции отдельный: max_context_chars ретривера на неё не влияет."""
    store = _FakeStore({"q0": [_bm25("a.py#f")]})
    pack = search_multi(_Retriever(store, _FakeEmbedder(), max_context_chars=100),
                        "o/n", ["q0"], limits=CodebaseLimits(), branch="dev")
    assert pack.max_chars == CodeSectionLimits().max_chars
