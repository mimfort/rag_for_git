from reviewer.graph.family import merge_signals


def test_family_finds_subclasses_by_inheritance():
    """Класс с наследниками отдаёт их как семейство по сигналу inheritance."""
    result = merge_signals("b.py#Base", ["a.py#One", "c.py#Two"], [])
    assert result.members == ["a.py#One", "c.py#Two"]
    assert result.signals == ["inheritance"]


def test_family_finds_protocol_implementers_structurally():
    """Protocol без номинальных наследников находит реализации структурно."""
    result = merge_signals("p.py#Proto", [], ["a.py#One", "b.py#Legacy"])
    assert result.members == ["a.py#One", "b.py#Legacy"]
    assert result.signals == ["structural"]


def test_implementations_points_at_family_when_it_exists():
    """Пустые прямые наследники + существующее семейство → не голая пустота.

    Именно этот случай воспроизводится на RestBoardBase: прямых наследников
    в графе может не быть, а семейство есть.
    """
    from reviewer.mcp.service import MCPReviewService

    msg = MCPReviewService._implementations_empty_message(family_size=8)
    assert "8" in msg
    assert "family" in msg


def test_implementations_stays_terse_when_no_family_either():
    """Ни наследников, ни семейства — прежний короткий ответ."""
    from reviewer.mcp.service import MCPReviewService

    msg = MCPReviewService._implementations_empty_message(family_size=0)
    assert msg == "(implementations не найдены)"


def test_family_puts_test_doubles_last_and_counts_them():
    """Тестовые дубли не теряются, но уходят в хвост и считаются в шапке.

    На живом графе TaskBoardProvider дал 11 адаптеров и 11 тестовых фейков:
    структурно они неотличимы, но при обрезке по cap выживать первыми должны
    продовые.
    """
    from reviewer.mcp.service import MCPReviewService

    members = [
        "tests/mcp/test_finish_task.py#_Provider",
        "reviewer/tasks/boards/jira.py#JiraCloudBoard",
        "tests/tasks/boards/provider_fakes.py#FakeBoard",
        "reviewer/tasks/boards/trello.py#TrelloBoard",
    ]
    ordered = MCPReviewService._family_order(members)
    assert ordered[:2] == ["reviewer/tasks/boards/jira.py#JiraCloudBoard",
                           "reviewer/tasks/boards/trello.py#TrelloBoard"]
    assert set(ordered) == set(members)

    result = merge_signals("reviewer/tasks/boards/base.py#TaskBoardProvider",
                           [], members)
    header = MCPReviewService._family_header(result)
    assert "из них тестовых дублей: 2" in header


def test_family_header_omits_test_count_when_none():
    """Семейство без тестовых дублей шапку лишним хвостом не засоряет."""
    from reviewer.mcp.service import MCPReviewService

    result = merge_signals("b.py#Base", ["a.py#One"], [])
    assert "тестовых дублей" not in MCPReviewService._family_header(result)


def test_family_header_marks_skipped_structural_signal_even_with_members():
    """Minor 6: FamilyResult.complete используется — шапка не молчит про пропуск сигнала."""
    from reviewer.mcp.service import MCPReviewService

    result = merge_signals("c.py#Thin", ["a.py#Child"], [], structural_skipped=True)
    header = MCPReviewService._family_header(result)
    assert "из 1 членов" in header
    assert "тоньше" in header
