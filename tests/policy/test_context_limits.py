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
    assert cl.code_section.max_files == 20
    assert cl.code_section.max_chunks_per_file == 1
    assert cl.code_section.chars_per_file == 975


def test_code_section_max_chars_is_derived():
    """Потолок производный и с запасом на рендер: он страховка, не бюджет."""
    assert CodeSectionLimits().max_chars == 20 * 975 * 3 // 2
    assert CodeSectionLimits(max_files=20, chars_per_file=600).max_chars == 18000


def test_code_section_max_chars_accounts_for_chunks_per_file():
    """При max_chunks_per_file>1 операционный бюджет вдвое больше — страховка обязана расти тоже."""
    assert (CodeSectionLimits(max_chunks_per_file=2).max_chars
            == 20 * 2 * 975 * 3 // 2)


def test_code_section_partial_block_keeps_other_defaults():
    cl = ContextLimits.from_review_yaml(
        {"context_limits": {"code_section": {"max_files": 30}}})
    assert cl.code_section.max_files == 30
    assert cl.code_section.chars_per_file == 975        # дефолт сохранён
    assert cl.code_section.max_chunks_per_file == 1


def test_code_section_is_independent_of_search_codebase():
    """Бюджет секции code не связан с чанковым потолком общего поиска."""
    cl = ContextLimits.from_review_yaml(
        {"context_limits": {"search_codebase": {"ceiling": 30}}})
    assert cl.search_codebase.ceiling == 30
    assert cl.code_section == CodeSectionLimits()


def test_code_section_operational_budget_matches_agreed_swap():
    """Критерий 5 PRI-259: бюджет вырос ровно на согласованную величину, не больше.

    Обмен ширины на глубину (12×1300 → 20×975) при неизменном произведении не
    брал порог bulk core-recall 0.55 (замер, ярус A: 16×975, бюджет 15600,
    медиана 0.4444). Порог берёт только рост самого произведения (ярус B:
    20×975, бюджет 19500, медиана 0.5833) при неизменной общей медиане
    (0.7500). Это осознанный размен критерия 5 PRI-259 (бюджет НЕ остался
    неизменным — он вырос), оплаченный взятием порога 0.55 по критерию 1
    (bulk 0.4000 → 0.5833): бюджет вырос с 15600 до 19500 символов — на 25 % —
    и не более этого. Имя теста намеренно не «did_not_grow»: бюджет вырос,
    рост согласован и ограничен сверху — это и проверяется.
    """
    lim = CodeSectionLimits()
    budget = lim.max_files * lim.max_chunks_per_file * lim.chars_per_file
    assert budget <= 19_500, (
        f"операционный бюджет секции вырос до {budget} против согласованных 19500 "
        "(15600 до PRI-259, +25% — принятая цена взятия порога bulk 0.55)"
    )


def test_code_section_chars_per_file_respects_depth_floor():
    """Пол `chars_per_file >= 975` — отдельный инвариант, не следствие проверки бюджета.

    Пол взят не из метрики (core-recall считает пути, а не содержимое блока —
    справочная точка `20×780`, ниже пола, дала числа, тождественные `20×975`,
    то есть замер к глубине фрагмента слеп), а из требования «в блоке читаются
    сигнатура символа и несколько строк тела» (`reviewer/policy/context_limits.py`,
    докстринг `CodeSectionLimits`). Проверка бюджета одна пол не ловит: точка
    `30×1×650` укладывается в те же согласованные 19500 символов операционного
    бюджета, что и выбранная `20×1×975`, но `chars_per_file=650` ниже пола —
    сокращение глубины ради ширины сверх согласованного обмена, которое
    метрика не заметила бы, а тело символа в блоке перестало бы помещаться.
    """
    lim = CodeSectionLimits()
    assert lim.chars_per_file >= 975, (
        f"дефолт chars_per_file={lim.chars_per_file} ниже пола глубины 975"
    )

    below_floor = CodeSectionLimits(max_files=30, chars_per_file=650)
    budget = (below_floor.max_files * below_floor.max_chunks_per_file
              * below_floor.chars_per_file)
    assert budget <= 19_500, "контрпример должен укладываться в тот же согласованный бюджет"
    assert below_floor.chars_per_file < 975, (
        "контрпример обязан нарушать пол глубины, иначе он не демонстрирует дыру "
        "между проверкой бюджета и проверкой пола"
    )


def test_code_section_augmented_quota_default_and_override():
    from reviewer.policy.context_limits import CodeSectionLimits, ContextLimits
    assert CodeSectionLimits().max_augmented_files == 3
    limits = ContextLimits.from_review_yaml(
        {"context_limits": {"code_section": {"max_augmented_files": 5}}})
    assert limits.code_section.max_augmented_files == 5
    assert limits.code_section.max_files == 20, "прочие ключи остаются дефолтными"
