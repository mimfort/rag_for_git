"""Каноническая структура описания задачи (PRI-213)."""
from reviewer.tasks.taskdoc import TaskDoc, render_markdown


def _doc(**kw):
    base = dict(title="Заголовок", problem="Суть проблемы",
                steps=["Первый шаг", "Второй шаг"],
                criteria=["Первый критерий"], context="Ссылка на спеку")
    base.update(kw)
    return TaskDoc(**base)


def test_render_sections_in_canonical_order():
    md = render_markdown(_doc())
    assert md.index("## Проблема") < md.index("## Что сделать")
    assert md.index("## Что сделать") < md.index("## Критерии приёмки")
    assert md.index("## Критерии приёмки") < md.index("## Контекст")


def test_title_is_not_part_of_the_body():
    # заголовок — отдельное поле задачи на доске, в описание он не дублируется
    assert "Заголовок" not in render_markdown(_doc())


def test_steps_and_criteria_are_numbered():
    md = render_markdown(_doc())
    assert "1. Первый шаг" in md
    assert "2. Второй шаг" in md
    assert "1. Первый критерий" in md


def test_empty_sections_are_omitted():
    md = render_markdown(_doc(context=None, criteria=[]))
    assert "## Контекст" not in md
    assert "## Критерии приёмки" not in md
    assert "## Проблема" in md


def test_blank_items_are_dropped_and_whitespace_trimmed():
    md = render_markdown(_doc(steps=["  Шаг с пробелами  ", "", "   "]))
    assert "1. Шаг с пробелами" in md
    assert "2." not in md


def test_fully_empty_doc_renders_empty_string():
    assert render_markdown(TaskDoc(title="Только заголовок")) == ""
