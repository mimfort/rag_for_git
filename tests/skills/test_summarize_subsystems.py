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


def test_skill_reports_deferred():
    """Шаг 5 должен упоминать deferred и cap — чтобы скилл не усекал молча."""
    text = SKILL.read_text(encoding="utf-8")
    assert "deferred" in text, "скилл не упоминает deferred"
    assert "SUMMARY_REBUILD_CAP" in text, "скилл не упоминает env-настройку cap"


def test_skill_asks_model_choice():
    """Скилл должен предлагать выбор модели для сводок (шаг 3)."""
    text = SKILL.read_text(encoding="utf-8")
    assert "model" in text.lower(), "скилл не упоминает выбор модели"
    # Хотя бы один дешёвый вариант должен быть упомянут как дефолт
    assert any(m in text for m in ("Haiku", "Sonnet", "Fable")), (
        "скилл не предлагает дешёвую модель по умолчанию"
    )


def test_skill_dispatches_subagent_on_chosen_model():
    """Шаг 4 должен описывать диспатч субагента на выбранной модели."""
    text = SKILL.read_text(encoding="utf-8")
    assert "subagent" in text.lower(), "скилл не упоминает субагент"
    assert "chosen" in text.lower() or "override" in text.lower(), (
        "скилл не упоминает override модели субагента"
    )


def test_skill_has_five_pipeline_steps():
    """Пайплайн должен содержать шаг 5 (Report) — прежний шаг 4 сдвинут."""
    import re
    text = SKILL.read_text(encoding="utf-8")
    # Ищем нумерованные шаги внутри ## Pipeline
    pipeline_section = re.search(r"## Pipeline(.*?)## Grounding", text, re.DOTALL)
    assert pipeline_section, "не найдена секция Pipeline"
    steps = re.findall(r"^\d+\.", pipeline_section.group(1), re.MULTILINE)
    assert len(steps) >= 5, f"ожидалось ≥5 шагов, найдено {len(steps)}"
