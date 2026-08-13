from __future__ import annotations

import pytest

from scripts.changelog_section import SectionNotFoundError, extract_section

CHANGELOG = """# Changelog

Все заметные изменения проекта.

## 0.4.10 — 2026-08-12

### Что нового
- Режимы solve-task.

### Исправлено
- Источник repo-тега.

## 0.4.1 — 2026-06-01

### Исправлено
- Ранний фикс.

## 0.4.0 — 2026-05-20

Первый релиз с нотами.
"""


def test_extract_middle_section() -> None:
    assert extract_section(CHANGELOG, "0.4.1") == (
        "### Исправлено\n- Ранний фикс."
    )


def test_extract_first_section() -> None:
    body = extract_section(CHANGELOG, "0.4.10")
    assert body.startswith("### Что нового")
    assert "Источник repo-тега." in body
    assert "0.4.1" not in body


def test_extract_last_section() -> None:
    assert extract_section(CHANGELOG, "0.4.0") == "Первый релиз с нотами."


def test_prefix_version_is_not_confused_with_longer_one() -> None:
    """0.4.1 не должна матчить заголовок 0.4.10 — иначе релиз получит чужие ноты."""
    assert "Ранний фикс." in extract_section(CHANGELOG, "0.4.1")
    assert "Ранний фикс." not in extract_section(CHANGELOG, "0.4.10")


def test_missing_version_raises() -> None:
    with pytest.raises(SectionNotFoundError):
        extract_section(CHANGELOG, "9.9.9")


def test_heading_without_date_is_accepted() -> None:
    text = "## 1.0.0\n\nБез даты.\n"
    assert extract_section(text, "1.0.0") == "Без даты."


def test_version_tag_prefix_is_stripped() -> None:
    assert extract_section(CHANGELOG, "v0.4.0") == "Первый релиз с нотами."


def test_empty_section_raises() -> None:
    text = "## 1.0.0 — 2026-01-01\n\n## 0.9.0 — 2025-12-01\n\nСтарое.\n"
    with pytest.raises(SectionNotFoundError):
        extract_section(text, "1.0.0")
