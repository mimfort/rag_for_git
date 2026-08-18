"""Guard-тесты изоляции пути solve-task от общего ретрива (PRI-259, критерий 3).

Секция code брифа живёт на своём бюджете (CodeSectionLimits) и своём
мультизапросном пути (search_multi). Общий Retriever.search_base обслуживает
/ask, грунтовку и ревью PR — он обязан остаться незатронутым. Оба теста
структурные: они ловят не поведение, а протечку зависимости.
"""
import inspect
import pathlib

from reviewer.retrieval import multiquery, retriever


def test_search_base_does_not_read_code_section_limits():
    """search_base не знает про файловый бюджет секции code."""
    source = inspect.getsource(retriever.Retriever.search_base)
    assert "CodeSectionLimits" not in source
    assert "section_limits" not in source
    assert "section_limits" not in inspect.signature(
        retriever.Retriever.search_base).parameters


def test_search_multi_is_called_only_from_the_task_context_path():
    """Единственный продакшн-вызов search_multi — приватный _search_codebase_multi."""
    root = pathlib.Path(multiquery.__file__).parent.parent  # пакет reviewer/
    callers = set()
    for path in root.rglob("*.py"):
        if path.name == "multiquery.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "search_multi(" in text:
            callers.add(path.relative_to(root).as_posix())
    assert callers == {"mcp/service.py"}, (
        f"search_multi вызывается из {sorted(callers)}; путь solve-task должен "
        "оставаться единственным потребителем — иначе /ask и ревью PR меняют поведение"
    )
