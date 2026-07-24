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


def test_canonical_task_document_round_trip_preserves_semantic_adf() -> None:
    markdown = (
        "## Проблема\n\n"
        "Нужна [спецификация](https://example.test/spec).\n\n"
        "- первый\n- второй\n\n"
        "1. раз\n2. два\n\n"
        "> Важная цитата\n\n"
        "```python\nprint('ok')\n```\n"
    )

    adf = markdown_to_adf(markdown)
    rendered = adf_to_markdown(adf.value).value
    restored = markdown_to_adf(rendered).value

    assert rendered == markdown.rstrip("\n")
    assert restored == adf.value
    assert "heading" in str(restored)
    assert adf_contains_link(restored, "https://example.test/spec")


def test_round_trip_escapes_literal_markdown_delimiters_and_backslashes() -> None:
    document = _doc(
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": r"literal \\ * ** \` [x] (y) # заголовок"}],
        }
    )

    markdown = adf_to_markdown(document).value

    assert r"\*" in markdown and r"\[x\]" in markdown and r"\(y\)" in markdown
    assert markdown != document["content"][0]["content"][0]["text"]
    assert markdown_to_adf(markdown).value == document


def test_round_trip_preserves_overlapping_marks_in_deterministic_order() -> None:
    document = _doc(
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "оба", "marks": [{"type": "em"}, {"type": "strong"}]},
                {"type": "text", "text": "код", "marks": [{"type": "link", "attrs": {"href": "https://e/x"}}, {"type": "code"}]},
            ],
        }
    )

    markdown = adf_to_markdown(document).value
    restored = markdown_to_adf(markdown).value

    assert markdown == "***оба***[`код`](https://e/x)"
    assert restored == _doc(
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "оба", "marks": [{"type": "strong"}, {"type": "em"}]},
                {"type": "text", "text": "код", "marks": [{"type": "code"}, {"type": "link", "attrs": {"href": "https://e/x"}}]},
            ],
        }
    )


def test_round_trip_preserves_escaped_link_label_and_href_identity() -> None:
    href = r"https://example.test/a)\\b"
    document = _doc(
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": r"ссылка [x)\\", "marks": [{"type": "link", "attrs": {"href": href}}]}],
        }
    )

    restored = markdown_to_adf(adf_to_markdown(document).value).value

    assert restored == document
    assert adf_contains_link(restored, href)


def test_round_trip_preserves_nested_lists_and_multiple_item_paragraphs() -> None:
    document = _doc(
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "первый"}]},
                        {"type": "paragraph", "content": [{"type": "text", "text": "второй абзац"}]},
                        {
                            "type": "orderedList",
                            "content": [
                                {
                                    "type": "listItem",
                                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "вложенный"}]}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
    )

    markdown = adf_to_markdown(document).value

    assert markdown == "- первый\n\n  второй абзац\n\n  1. вложенный"
    assert markdown_to_adf(markdown).value == document


@pytest.mark.parametrize(
    ("code", "fence_length"),
    [
        ("до\n" + chr(96) * 3 + "\nпосле", 4),
        (chr(96) * 5, 6),
    ],
)
def test_code_block_round_trip_uses_fence_longer_than_content(code: str, fence_length: int) -> None:
    document = _doc(
        {
            "type": "codeBlock",
            "attrs": {"language": "python"},
            "content": [{"type": "text", "text": code}],
        }
    )

    markdown = adf_to_markdown(document).value
    fence = chr(96) * fence_length

    assert markdown == f"{fence}python\n{code}\n{fence}"
    assert markdown_to_adf(markdown).value == document


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
