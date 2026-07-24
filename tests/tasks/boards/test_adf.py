"""Чистая конвертация Jira ADF и канонического markdown (PRI-215)."""
from __future__ import annotations

import pytest

from reviewer.tasks.boards.adf import (
    adf_contains_link,
    adf_to_markdown,
    append_link_paragraph,
    markdown_to_adf,
)


def _doc(*content: dict) -> dict:
    return {"type": "doc", "version": 1, "content": list(content)}


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        (
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "первая"},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "вторая"},
                ],
            },
            "первая\nвторая",
        ),
        (
            {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Тема"}]},
            "### Тема",
        ),
        (
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "раз"}]}]},
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "два"}]}]},
                ],
            },
            "- раз\n- два",
        ),
        (
            {
                "type": "orderedList",
                "content": [
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "раз"}]}]},
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "два"}]}]},
                ],
            },
            "1. раз\n2. два",
        ),
        (
            {"type": "blockquote", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "цитата"}]}]},
            "> цитата",
        ),
        (
            {"type": "codeBlock", "attrs": {"language": "python"}, "content": [{"type": "text", "text": "print(1)"}]},
            "```python\nprint(1)\n```",
        ),
        (
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "жирный", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": " "},
                    {"type": "text", "text": "курсив", "marks": [{"type": "em"}]},
                    {"type": "text", "text": " "},
                    {"type": "text", "text": "код", "marks": [{"type": "code"}]},
                    {"type": "text", "text": " "},
                    {"type": "text", "text": "ссылка", "marks": [{"type": "link", "attrs": {"href": "https://e/x"}}]},
                ],
            },
            "**жирный** *курсив* `код` [ссылка](https://e/x)",
        ),
    ],
)
def test_adf_to_markdown_canonical_nodes(node: dict, expected: str) -> None:
    assert adf_to_markdown(_doc(node)).value == expected


def test_canonical_task_document_round_trip_preserves_markdown_except_terminal_newline() -> None:
    markdown = (
        "## Проблема\n\n"
        "Нужна [спецификация](https://example.test/spec).\n\n"
        "- первый\n- второй\n\n"
        "1. раз\n2. два\n\n"
        "> Важная цитата\n\n"
        "```python\nprint('ok')\n```\n"
    )

    adf = markdown_to_adf(markdown)
    restored = adf_to_markdown(adf.value).value

    assert restored == markdown.rstrip("\n")
    assert "heading" in str(adf.value)
    assert adf_contains_link(adf.value, "https://example.test/spec")


def test_unknown_node_preserves_recursive_text_and_reports_type() -> None:
    document = _doc({"type": "panel", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "не потерять"}]}]})

    converted = adf_to_markdown(document)

    assert converted.value == "не потерять"
    assert any("panel" in warning for warning in converted.warnings)


def test_link_identity_uses_href_not_visible_label_and_append_is_immutable() -> None:
    document = _doc(
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "другая подпись", "marks": [{"type": "link", "attrs": {"href": "https://g/p/1"}}]}],
        }
    )
    original_content = document["content"].copy()

    assert adf_contains_link(document, "https://g/p/1")
    assert not adf_contains_link(document, "другая подпись")
    assert append_link_paragraph(document, "https://g/p/1", label="PR: https://g/p/1", note=None) == document

    appended = append_link_paragraph(document, "https://g/p/2", label="PR: https://g/p/2", note="закрыто автоматически")
    assert appended is not document
    assert document["content"] == original_content
    assert adf_contains_link(appended, "https://g/p/2")
    assert adf_to_markdown(appended).value.endswith("[PR: https://g/p/2](https://g/p/2)\n\nзакрыто автоматически")
