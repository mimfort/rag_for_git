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
