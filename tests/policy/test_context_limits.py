from reviewer.policy.context_limits import ContextLimits, CodebaseLimits


def test_defaults_when_no_block():
    cl = ContextLimits.from_review_yaml({})
    assert cl.search_codebase == CodebaseLimits()
    assert cl.search_codebase.candidate_pool == 30
    assert cl.search_tasks.ceiling == 8
    assert cl.graph.hops == 1 and cl.graph.callers_topk == 25


def test_partial_block_keeps_other_defaults():
    cl = ContextLimits.from_review_yaml(
        {"context_limits": {"search_codebase": {"ceiling": 30, "ratio": 0.4}}})
    assert cl.search_codebase.ceiling == 30
    assert cl.search_codebase.ratio == 0.4
    assert cl.search_codebase.floor == 4          # дефолт сохранён
    assert cl.search_codebase.abs_floor == 0.3
    assert cl.search_tasks.ceiling == 8           # подсекции не заданы → дефолт


def test_subsections_search_tasks_and_graph():
    cl = ContextLimits.from_review_yaml(
        {"context_limits": {"search_tasks": {"ceiling": 12}, "graph": {"hops": 2}}})
    assert cl.search_tasks.ceiling == 12
    assert cl.graph.hops == 2
    assert cl.graph.callers_topk == 25
