from reviewer.policy.context_limits import CodeSectionLimits, ContextLimits, CodebaseLimits


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


def test_code_section_defaults():
    cl = ContextLimits.from_review_yaml({})
    assert cl.code_section == CodeSectionLimits()
    assert cl.code_section.max_files == 12
    assert cl.code_section.max_chunks_per_file == 1
    assert cl.code_section.chars_per_file == 1300


def test_code_section_max_chars_is_derived():
    """Потолок производный и с запасом на рендер: он страховка, не бюджет."""
    assert CodeSectionLimits().max_chars == 12 * 1300 * 3 // 2
    assert CodeSectionLimits(max_files=20, chars_per_file=600).max_chars == 18000


def test_code_section_partial_block_keeps_other_defaults():
    cl = ContextLimits.from_review_yaml(
        {"context_limits": {"code_section": {"max_files": 20}}})
    assert cl.code_section.max_files == 20
    assert cl.code_section.chars_per_file == 1300        # дефолт сохранён
    assert cl.code_section.max_chunks_per_file == 1


def test_code_section_is_independent_of_search_codebase():
    """Бюджет секции code не связан с чанковым потолком общего поиска."""
    cl = ContextLimits.from_review_yaml(
        {"context_limits": {"search_codebase": {"ceiling": 30}}})
    assert cl.search_codebase.ceiling == 30
    assert cl.code_section == CodeSectionLimits()
