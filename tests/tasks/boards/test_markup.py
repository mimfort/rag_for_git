"""Конвертеры разметки для досок, хранящих описание в HTML (PRI-213)."""
from reviewer.tasks.boards.markup import html_to_md, md_to_html


def test_headings_become_h2_h3():
    assert "<h2>Проблема</h2>" in md_to_html("## Проблема")
    assert "<h3>Деталь</h3>" in md_to_html("### Деталь")


def test_paragraphs_wrapped_in_p():
    html = md_to_html("Первый абзац\n\nВторой абзац")
    assert "<p>Первый абзац</p>" in html
    assert "<p>Второй абзац</p>" in html


def test_numbered_and_bullet_lists():
    ol = md_to_html("1. Раз\n2. Два")
    assert "<ol>" in ol and "<li>Раз</li>" in ol and "<li>Два</li>" in ol
    ul = md_to_html("- Пункт\n- Ещё")
    assert "<ul>" in ul and "<li>Пункт</li>" in ul


def test_inline_code_content_is_escaped():
    # HTML внутри инлайн-кода должен доехать до доски как ВИДИМЫЙ текст, не как тег
    html = md_to_html("перенос строки это `<br />`")
    assert "<code>&lt;br /&gt;</code>" in html
    assert "<br />" not in html.replace("&lt;br /&gt;", "")


def test_fenced_code_block():
    html = md_to_html("```\nprint(1)\n```")
    assert "<pre><code>" in html and "print(1)" in html


def test_link_and_bold():
    html = md_to_html("см. [спеку](https://e/x) и **важное**")
    assert '<a href="https://e/x">спеку</a>' in html
    assert "<strong>важное</strong>" in html


def test_raw_html_in_plain_text_is_escaped():
    html = md_to_html("текст с <script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_empty_input_returns_empty():
    assert md_to_html("") == ""


def test_link_with_dangerous_scheme_is_not_rendered_as_link():
    # javascript:/data: в href — XSS-вектор; такая ссылка должна деградировать
    # в обычный текст, а не стать активным <a href="...">
    for url in ("javascript:alert(1)", "data:text/html,<script>alert(1)</script>"):
        html_out = md_to_html(f"[click]({url})")
        assert "<a " not in html_out
        assert "href=" not in html_out
        assert "<script>" not in html_out


PRI_213 = """## Проблема

Reviewer читает доски по REST и пишет узко.

## Что сделать

1. Добавить тул `create_task`
2. Реализовать в провайдерах

## Критерии приёмки

1. Тул создаёт задачу и возвращает ключ"""


def test_round_trip_preserves_canonical_markdown():
    assert html_to_md(md_to_html(PRI_213)) == PRI_213


def test_plain_markdown_passes_through_unchanged():
    # почти все задачи на доске — markdown, лежащий в HTML-поле как обычный текст
    assert html_to_md(PRI_213) == PRI_213


def test_entities_are_unescaped_in_plain_text():
    assert html_to_md("created_at &gt; now()") == "created_at > now()"


def test_finish_task_pr_block_becomes_plain_line():
    src = 'тело<div>PR: <a href="https://g/p/1">https://g/p/1</a></div>'
    md = html_to_md(src)
    assert "<div>" not in md and "<a" not in md
    assert "PR: https://g/p/1" in md
    assert "тело" in md


def test_br_becomes_newline_and_unknown_tags_are_transparent():
    md = html_to_md("первая<br />вторая<span>третья</span>")
    assert "<br" not in md and "<span>" not in md
    assert md.splitlines()[0] == "первая"
    assert "третья" in md


def test_named_link_becomes_markdown_link():
    assert html_to_md('<a href="https://e/x">спека</a>') == "[спека](https://e/x)"


def test_lists_become_markdown_lists():
    md = html_to_md("<ul><li>раз</li><li>два</li></ul>")
    assert md == "- раз\n- два"
    md = html_to_md("<ol><li>раз</li><li>два</li></ol>")
    assert md == "1. раз\n2. два"


def test_broken_html_never_raises():
    assert html_to_md("<div><p>текст") .strip() == "текст"


def test_empty_input_returns_empty_md():
    assert html_to_md("") == ""
