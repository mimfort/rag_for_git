import re
from pathlib import Path

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "summarize-subsystems" / "SKILL.md")
_INCLUDE = re.compile(r"<!-- include: (_common/[A-Za-z0-9_-]+\.md) -->")


def _assembled_skill() -> str:
    text = SKILL.read_text(encoding="utf-8")
    skills_dir = SKILL.resolve().parents[1]
    return _INCLUDE.sub(
        lambda match: (skills_dir / match.group(1)).read_text(encoding="utf-8"),
        text,
    )


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
    # Фраза уникальна для шага 3 и отсутствует в SKILL.md за пределами этого шага
    assert "Ask the user which model tier to use for writing summaries" in text, (
        "шаг 3 не спрашивает пользователя о модели (уникальная фраза шага удалена)"
    )
    # Хотя бы один дешёвый вариант должен быть упомянут как дефолт
    assert any(m in text for m in ("Haiku", "Sonnet", "Fable")), (
        "скилл не предлагает дешёвую модель по умолчанию"
    )


def test_skill_dispatches_subagent_on_chosen_model():
    """Шаг 4 должен описывать диспатч субагента на выбранной модели."""
    text = SKILL.read_text(encoding="utf-8")
    # Фраза уникальна для шага 4 и отсутствует в SKILL.md за пределами этого шага
    assert "dispatch a subagent on the chosen" in text, (
        "шаг 4 не предписывает диспатч субагента на выбранной модели (уникальная фраза шага удалена)"
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


def test_skill_preflight_echoes_depth_and_confirms():
    text = SKILL.read_text(encoding="utf-8")
    assert "depth_source" in text, "preflight не эхо-ит depth_source"
    assert "SUMMARY_CLUSTER_DEPTH" in text, "preflight не упоминает env-настройку depth"
    assert "confirm" in text.lower(), "preflight не просит подтверждения перед прогоном"
    assert "full rebuild" in text.lower(), "нет предупреждения о полном пересборе при смене depth"


def test_skill_prunes_orphans_on_full_pass():
    text = SKILL.read_text(encoding="utf-8")
    assert "prune_subsystem_summaries" in text, "скилл не вызывает prune на полном прогоне"
    assert "orphan" in text.lower(), "скилл не упоминает осиротевшие сводки"


def test_skill_uses_incremental_file_summary_protocol():
    text = _assembled_skill()
    assert "get_subsystem_summary_work" in text
    assert "stale == true OR bootstrap == true" in text
    assert "added_files + changed_files" in text
    assert "one file-summary job" in text
    assert "must not read unchanged" in text.lower()
    assert "reused_fragments" in text
    assert "moved_files" in text


def test_skill_composes_only_from_ordered_fragment_texts():
    text = _assembled_skill()
    assert "ordered reused/moved/new fragment texts" in text
    assert "composer must not call `Read`" in text
    assert "source-code claims absent from the fragments" in text
    assert "file prompt must name only its own path" in text


def test_skill_persists_new_fragments_and_defers_races():
    text = _assembled_skill()
    normalized = " ".join(text.split())
    assert (
        "`index_subsystem_summary(repo, branch, cluster_key, title, summary, "
        "source_hash, fragments=[new file results])`"
        in normalized
    )
    assert "`stored=false`" in text
    assert "deferred/raced" in text
    assert "must not count it as success" in text


def test_skill_reports_incremental_metrics():
    text = _assembled_skill()
    for metric in (
        "created",
        "reused",
        "removed",
        "moved",
        "deferred",
        "raced",
        "fragments_pruned",
        "embedded",
    ):
        assert metric in text, f"скилл не сообщает метрику {metric}"
