"""Тесты конвертации YFM-разметки Yandex Tracker в markdown."""
from __future__ import annotations

from reviewer.tasks.boards.yfm import yfm_to_md


def test_code_block_without_language():
    assert yfm_to_md("до\n%%\nprint(1)\n%%\nпосле") == "до\n```\nprint(1)\n```\nпосле"


def test_code_block_with_language():
    assert yfm_to_md("%%(python)\nprint(1)\n%%") == "```python\nprint(1)\n```"


def test_inline_link_is_converted():
    assert yfm_to_md("см. ((https://example.test док))") == "см. [док](https://example.test)"


def test_link_without_text_keeps_url():
    assert yfm_to_md("((https://example.test))") == "<https://example.test>"


def test_cut_becomes_bold_heading_with_body():
    assert yfm_to_md("<{Детали\nтело\n}>") == "**Детали**\n\nтело"


def test_plain_markdown_is_unchanged():
    text = "# Заголовок\n\n- пункт\n\n`код`"
    assert yfm_to_md(text) == text


def test_unknown_constructs_are_left_as_is_and_never_raise():
    text = "!!(red)важно!!\n{{макрос}}"
    assert yfm_to_md(text) == text
    assert yfm_to_md("") == ""
