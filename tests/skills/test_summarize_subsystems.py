from pathlib import Path

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "summarize-subsystems" / "SKILL.md")


def test_skill_exists_and_mentions_tools():
    text = SKILL.read_text(encoding="utf-8")
    assert "list_subsystem_clusters" in text
    assert "index_subsystem_summary" in text


def test_skill_includes_common_blocks_that_exist():
    text = SKILL.read_text(encoding="utf-8")
    common = SKILL.resolve().parents[1] / "_common"
    import re
    includes = re.findall(r"<!-- include: (_common/[\w\-./]+) -->", text)
    assert includes, "нет include-маркеров _common"
    for inc in includes:
        assert (common.parent / inc).is_file(), f"include не найден: {inc}"


def test_skill_instructs_russian_output():
    text = SKILL.read_text(encoding="utf-8").lower()
    assert "russian" in text or "русск" in text
