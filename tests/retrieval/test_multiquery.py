"""RRF-слияние выдач подзапросов и обрезка блока рендера (PRI-255)."""
from reviewer.index.store import Retrieved
from reviewer.policy.context_limits import CodebaseLimits, CodeSectionLimits
from reviewer.retrieval.augment import AugmentSource
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
                 nodes_by_id: dict | None = None, nodes_by_path: dict | None = None):
        self._by_query = by_query
        self._fail_for = fail_for
        self._nodes_by_id = nodes_by_id or {}
        self._nodes_by_path = nodes_by_path or {}
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

    def fetch_retrieved_at_paths(self, repo, paths, *, base_ref, limit_per_path=1):
        return [self._nodes_by_path[p] for p in paths if p in self._nodes_by_path]


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
    assert len({it.path for it in pack.items}) == 20, "дефолт max_files, а не ceiling=5"


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
    assert diversify_by_file(items, max_files=5, max_chunks_per_file=0) == []


def test_dedupe_runs_before_diversify_keeps_enclosing_class():
    """Обязательный порядок (PRI-256): _dedupe_overlapping ДО diversify_by_file.

    Метод ранжирован выше своего охватывающего класса (лучший ранг в
    единственном прогоне), но при max_chunks_per_file=1 в выдаче обязан
    выжить класс — он же самый широкий чанк. Если diversify отработает
    первым, он оставит только метод (первый в порядке по рангу) и класс
    уже некому будет вытеснить его при дедупе.
    """
    method = _hit("a.py#Cls.method", start_line=40, end_line=55, text="method body")
    method.bm25_hit = True
    cls = _hit("a.py#Cls", start_line=10, end_line=90, text="class body")
    cls.bm25_hit = True
    store = _FakeStore({"q0": [method, cls]})  # метод первым — выше ранг в RRF
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    assert [it.node_id for it in pack.items] == ["a.py#Cls"], \
        "дедуп вложенных чанков обязан идти раньше файловой диверсификации"


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


def test_two_sources_get_separate_quotas_and_named_note():
    store = _FakeStore(
        {"q0": [_bm25("a.py#f")]},
        nodes_by_path={
            "x.py": _hit("x.py#s"), "y.py": _hit("y.py#s"),
            "s1.py": _hit("s1.py#s"), "s2.py": _hit("s2.py#s"),
        },
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[
            AugmentSource(name="similar-diffs", paths=["x.py", "y.py"], quota=1),
            AugmentSource(name="second-source", paths=["s1.py", "s2.py"], quota=1),
        ])
    paths = [it.path for it in pack.items]
    assert paths == ["a.py", "x.py", "s1.py"], "по одному файлу из каждого источника, гибрид первым"
    assert pack.augment_note == (
        "— подмешано 2 файлов: similar-diffs 1 (квота 1), second-source 1 (квота 1)")


def test_second_source_does_not_repeat_path_taken_by_first():
    store = _FakeStore(
        {"q0": [_bm25("a.py#f")]},
        nodes_by_path={"x.py": _hit("x.py#s"), "y.py": _hit("y.py#s")},
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[
            AugmentSource(name="similar-diffs", paths=["x.py"], quota=2),
            AugmentSource(name="second-source", paths=["x.py", "y.py"], quota=2),
        ])
    paths = [it.path for it in pack.items]
    assert paths.count("x.py") == 1, "путь первого источника второму не достаётся"
    assert "y.py" in paths
    assert pack.augment_note == (
        "— подмешано 2 файлов: similar-diffs 1 (квота 2), second-source 1 (квота 2)")


def test_augmented_paths_appended_after_hybrid_and_capped_by_quota():
    store = _FakeStore(
        {"q0": [_bm25("a.py#f")]},
        nodes_by_path={
            "x.py": _hit("x.py#s"), "y.py": _hit("y.py#s"), "z.py": _hit("z.py#s"),
        },
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(name="similar-diffs",
                                       paths=["x.py", "y.py", "z.py"], quota=2)])
    paths = [it.path for it in pack.items]
    assert paths[0] == "a.py", "гибрид остаётся первым"
    assert paths[1:] == ["x.py", "y.py"], "квота режет третий подмешанный файл"


def test_augmented_gets_reserved_slots_even_when_hybrid_overflows_budget():
    """PRI-257 (добор по step8-measurement.md): резерв, а не потолок на остаток.

    До фикса augmented — последний источник в списке, и diversify_by_file
    отдавал ему слот только если гибрид не успевал заполнить max_files сам:
    на 42 живых задачах это душило сигнал до 0-1 файла из выдачи. Теперь
    augmented получает свою квоту ГАРАНТИРОВАННО, гибрид — max_files минус
    фактически занятый резерв.
    """
    hits = [_bm25(f"f{i}.py#s") for i in range(20)]
    store = _FakeStore(
        {"q0": hits},
        nodes_by_path={
            "x.py": _hit("x.py#s"), "y.py": _hit("y.py#s"), "z.py": _hit("z.py#s"),
        },
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(name="similar-diffs",
                                       paths=["x.py", "y.py", "z.py"], quota=3)])
    paths = [it.path for it in pack.items]
    assert {"x.py", "y.py", "z.py"} <= set(paths), \
        "все 3 подмешанных файла в выдаче, несмотря на переполненный гибридом бюджет"
    assert len(paths) == CodeSectionLimits().max_files, \
        "общий файловый бюджет секции не превышен"


def test_hybrid_gets_full_budget_without_augmented_candidates():
    """Регресс-проверка: без augmented-кандидатов гибрид получает ПОЛНЫЙ max_files.

    Резерв не должен вычитаться из бюджета гибрида, когда подмешивать нечего —
    иначе задачи без путевого сигнала потеряли бы файлы просто так.
    """
    hits = [_bm25(f"f{i}.py#s") for i in range(20)]
    store = _FakeStore({"q0": hits})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    assert len({it.path for it in pack.items}) == CodeSectionLimits().max_files


def test_hybrid_budget_reduced_only_by_actually_used_augmented_slots():
    """Квота 3, но стор находит только 1 путь — резерв фактический (1), не номинальный (3).

    Требование: «гибрид и graph-only делят max_files минус фактически занятый
    резерв (не минус номинальную квоту)».
    """
    hits = [_bm25(f"f{i}.py#s") for i in range(20)]
    store = _FakeStore({"q0": hits}, nodes_by_path={"x.py": _hit("x.py#s")})
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(name="similar-diffs",
                                       paths=["x.py", "нет-в-сторе.py", "тоже-нет.py"],
                                       quota=3)])
    paths = [it.path for it in pack.items]
    assert "x.py" in paths
    assert len(paths) == CodeSectionLimits().max_files, \
        "резерв фактический (1 найденный путь), а не номинальные 3 из квоты"


def test_augmented_promotes_low_ranked_hybrid_candidate_not_final_survivor():
    """PRI-257 (второй фикс по step8-measurement.md, «Повторный замер после резерва»).

    known_paths для augmented считается по ИТОГОВОЙ гибридной выдаче
    (после dedupe+diversify), а не по сырому пулу (merged+graph). Кандидат,
    которого гибрид НАШЁЛ (он есть в пуле из 30), но ранжировал ниже
    max_files=20 и потому не попал в выдачу, обязан подмешаться — раньше он
    считался «уже известным» по сырому пулу и молча отбрасывался.
    """
    hits = [_bm25(f"f{i}.py#s") for i in range(30)]
    store = _FakeStore({"q0": hits}, nodes_by_path={"f25.py": _hit("f25.py#s")})
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(name="similar-diffs", paths=["f25.py"], quota=1)])
    paths = [it.path for it in pack.items]
    assert "f25.py" in paths, \
        "низкоранжированный, но найденный гибридом путь промоутится, а не теряется"
    assert len(paths) == CodeSectionLimits().max_files


def test_augmented_path_already_in_final_output_is_not_duplicated_or_reserved():
    """Кандидат, реально попавший в итоговую гибридную выдачу — не дублируется.

    known_paths по итоговой выдаче обязан исключать такие пути: иначе резерв
    тратился бы впустую на файл, который и так уже есть в результате.
    """
    hits = [_bm25(f"f{i}.py#s") for i in range(5)]
    store = _FakeStore({"q0": hits}, nodes_by_path={"f0.py": _hit("f0.py#other")})
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(name="similar-diffs", paths=["f0.py"], quota=3)])
    paths = [it.path for it in pack.items]
    assert paths.count("f0.py") == 1, "нет дубля"
    assert pack.augment_note is None, "путь уже в выдаче — резерв не тратится, ноты нет"


def test_promoted_augmented_candidates_respect_quota_and_total_budget():
    """Промоутинг из хвоста пула всё равно ограничен квотой и общим max_files."""
    hits = [_bm25(f"f{i}.py#s") for i in range(30)]
    store = _FakeStore(
        {"q0": hits},
        nodes_by_path={f"f{i}.py": _hit(f"f{i}.py#s") for i in (25, 26, 27, 28)},
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(name="similar-diffs",
                                       paths=["f25.py", "f26.py", "f27.py", "f28.py"],
                                       quota=2)])
    paths = [it.path for it in pack.items]
    promoted = [p for p in ("f25.py", "f26.py", "f27.py", "f28.py") if p in paths]
    assert len(promoted) == 2, "квота 2 режет остальных кандидатов из хвоста пула"
    assert len(paths) == CodeSectionLimits().max_files


def test_augmented_paths_not_found_in_store_leave_no_trace():
    """augment_paths непуст, но стор ничего не нашёл по этим путям — нет ни ноты, ни файлов.

    Требуемый путь не проиндексирован в base (или ещё не догнал base-индекс);
    _augment_items уже фильтрует пустой fetched, но факт нужно закрепить тестом
    на уровне search_multi (PRI-257, добор ревью task 3)."""
    store = _FakeStore({"q0": [_bm25("a.py#f")]}, nodes_by_path={})
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(name="similar-diffs", paths=["x.py"], quota=3)])
    assert pack.augment_note is None
    assert "x.py" not in {it.path for it in pack.items}


def test_augment_quota_counts_files_with_chunks_not_candidates_considered():
    """PRI-257 (третий фикс по step8-measurement.md, «Третий замер»).

    Первые кандидаты (docs/*, README) физически не имеют чанков — квота не
    обязана выгорать на них. .py-кандидаты из хвоста списка обязаны
    подмешаться, если у них есть чанки, ровно в количестве quota.
    """
    store = _FakeStore(
        {"q0": [_bm25("a.py#f")]},
        nodes_by_path={"c.py": _hit("c.py#s"), "d.py": _hit("d.py#s")},
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(
            name="similar-diffs",
            paths=["docs/a.md", "docs/b.md", "README.md", "c.py", "d.py"], quota=2)])
    paths = [it.path for it in pack.items]
    assert {"c.py", "d.py"} <= set(paths), \
        ".py-кандидаты из хвоста подмешиваются, хотя перед ними стояли бесчанковые пути"
    assert "подмешано 2" in pack.as_context()


def test_augment_quota_not_fully_used_when_fewer_chunked_candidates_exist():
    """Кандидатов с чанками меньше квоты — подмешиваются все найденные, нота честная."""
    store = _FakeStore({"q0": [_bm25("a.py#f")]}, nodes_by_path={"c.py": _hit("c.py#s")})
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(name="similar-diffs",
                                       paths=["docs/a.md", "docs/b.md", "c.py"], quota=3)])
    paths = [it.path for it in pack.items]
    assert "c.py" in paths
    assert "подмешано 1" in pack.as_context(), "нота считает фактически найденное, не квоту"


class _FailingFetchStore(_FakeStore):
    def fetch_retrieved_at_paths(self, repo, paths, *, base_ref, limit_per_path=1):
        raise RuntimeError("стор недоступен")


def test_augment_store_fetch_failure_is_fail_soft():
    """Сбой fetch_retrieved_at_paths (уже собранный список кандидатов) — не роняет сборку."""
    store = _FailingFetchStore({"q0": [_bm25("a.py#f")]})
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(name="similar-diffs", paths=["x.py"], quota=2)])
    assert pack.augment_note is None
    assert [it.path for it in pack.items] == ["a.py"]


def test_augment_note_reports_source_and_quota():
    """Co-change снят (PRI-257, приёмка по step8-measurement.md) — нота больше не
    разбивает подмешанное по источникам, только similar-diffs и квота."""
    store = _FakeStore({"q0": [_bm25("a.py#f")]},
                       nodes_by_path={"x.py": _hit("x.py#s")})
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(name="similar-diffs", paths=["x.py"], quota=3)])
    context = pack.as_context()
    assert "подмешано 1" in context
    assert "similar-diffs" in context and "квота 3" in context
    assert "co-change" not in context


def test_augment_quota_skips_test_paths_before_counting_when_tests_excluded():
    """Финальное ревью ветки, Important: квота не выгорает на тестовых путях.

    include_tests=False — прод-дефолт секции code. Список подмешиваемых путей
    из git-фолбэка (paths_touched_by_grep) не фильтрует core/test — если
    tests/test_foo.py стоит раньше reviewer/foo.py, постфактум-фильтр съедал
    уже занятый тестами слот квоты. Фильтр обязан идти ДО подсчёта квоты:
    полезные .py-кандидаты из хвоста получают слот, тестовые не подмешиваются
    вовсе.
    """
    store = _FakeStore(
        {"q0": [_bm25("a.py#f")]},
        nodes_by_path={
            "tests/test_x.py": _hit("tests/test_x.py#t"),
            "tests/test_y.py": _hit("tests/test_y.py#t"),
            "c.py": _hit("c.py#s"),
        },
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(
            name="similar-diffs",
            paths=["tests/test_x.py", "tests/test_y.py", "c.py"], quota=1)])
    paths = [it.path for it in pack.items]
    assert "c.py" in paths, "квота досталась не-тестовому кандидату из хвоста"
    assert "tests/test_x.py" not in paths and "tests/test_y.py" not in paths


def test_augment_allows_test_paths_when_include_tests_requested():
    """Обратный случай: include_tests=True — тестовые пути допустимы к подмешиванию."""
    store = _FakeStore(
        {"q0": [_bm25("a.py#f")]},
        nodes_by_path={"tests/test_x.py": _hit("tests/test_x.py#t")},
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(), branch="dev",
        augment_sources=[AugmentSource(name="similar-diffs",
                                       paths=["tests/test_x.py"], quota=1)],
        include_tests=True)
    paths = [it.path for it in pack.items]
    assert "tests/test_x.py" in paths


def test_no_augmentation_leaves_note_absent():
    store = _FakeStore({"q0": [_bm25("a.py#f")]})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    assert pack.augment_note is None
    assert "подмешано" not in pack.as_context()


def test_augmented_candidates_ordered_by_raw_pool_rank():
    """Файл кластера, найденный гибридом (пусть и низко), идёт раньше ненайденного."""
    hits = [_bm25(f"f{i}.py#s") for i in range(20)] + [_bm25("late.py#s")]
    store = _FakeStore(
        {"q0": hits},
        nodes_by_path={"late.py": _hit("late.py#s"), "never.py": _hit("never.py#s")},
    )
    pack = search_multi(
        _Retriever(store, _FakeEmbedder()), "o/n", ["q0"], limits=CodebaseLimits(),
        section_limits=CodeSectionLimits(max_augmented_files=0),
        branch="dev",
        augment_sources=[AugmentSource(name="second-source",
                                       paths=["never.py", "late.py"], quota=1)])
    paths = [it.path for it in pack.items]
    assert "late.py" in paths, "низко ранжированный гибридом файл приоритетнее ненайденного"
    assert "never.py" not in paths
