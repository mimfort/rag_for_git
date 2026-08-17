"""RRF-слияние выдач подзапросов и обрезка блока рендера (PRI-255)."""
from reviewer.index.store import Retrieved
from reviewer.retrieval.multiquery import MAX_BLOCK_CHARS, cap_block, rrf_merge


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
