# tests/skills/test_pr_walkthrough_skill.py
from pathlib import Path
import re

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "pr-walkthrough" / "SKILL.md")


def test_skill_exists_and_uses_session_tools():
    text = SKILL.read_text(encoding="utf-8")
    assert "prepare_review" in text
    assert "get_impact" in text
    assert "find_callers" in text


def test_skill_includes_resolve_to_existing_common_files():
    text = SKILL.read_text(encoding="utf-8")
    includes = re.findall(r"<!-- include: (_common/[\w\-./]+) -->", text)
    assert includes, "нет include-маркеров _common"
    base = SKILL.resolve().parents[1]
    for inc in includes:
        assert (base / inc).is_file(), f"include не найден: {inc}"


def test_skill_posting_is_opt_in_and_russian():
    text = SKILL.read_text(encoding="utf-8")
    assert "post_pr_walkthrough" in text
    low = text.lower()
    assert "russian" in low or "русск" in low
    # постинг — только по явной просьбе (outward-facing)
    assert "explicit" in low or "only on explicit" in low or "явн" in low
