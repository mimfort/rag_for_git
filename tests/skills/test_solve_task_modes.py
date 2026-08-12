"""Guardrail: стартовый опрос solve-task и профиль исполнения lite (PRI-243).

Тесты маркерные: пинят стабильные якоря спеки, а не формулировки, чтобы правка
промпта не удалила требование молча.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"
PROFILE = ROOT / "plugin" / "skills" / "_profiles" / "execution-lite.md"


def test_lite_profile_exists():
    assert PROFILE.is_file(), "нет plugin/skills/_profiles/execution-lite.md"
    assert PROFILE.read_text(encoding="utf-8").strip()


def test_lite_profile_is_a_delta_over_sdd():
    text = PROFILE.read_text(encoding="utf-8")
    assert "superpowers:subagent-driven-development" in text


def test_lite_profile_groups_reviews():
    text = PROFILE.read_text(encoding="utf-8")
    assert "at most 3" in text            # потолок размера группы
    assert "overlapping files" in text    # критерий склейки задач в группу


def test_lite_profile_lowers_fix_round_cap():
    text = PROFILE.read_text(encoding="utf-8")
    assert "3 fix rounds" in text
    assert "instead of 5" in text
    assert "round 3" in text              # эскалация модели сдвинута на раунд 3


def test_lite_profile_keeps_final_review_mandatory():
    text = PROFILE.read_text(encoding="utf-8")
    assert "final whole-branch review" in text
    assert "mandatory" in text
    assert "never disabled" in text


def test_lite_profile_has_no_own_machinery():
    # Профиль — список дельт, а не исполнитель: он не переопределяет ledger,
    # BASE-трекинг и собственный цикл, а ссылается на SDD.
    text = PROFILE.read_text(encoding="utf-8")
    assert "# SDD ledger" not in text        # формат ledger не переопределяется
    assert "git rev-parse HEAD" not in text  # рецепт BASE-трекинга не дублируется
    assert "unchanged from superpowers:subagent-driven-development" in text


def _survey_section() -> str:
    """Вырезать подпункт 0 Шага 0 — от заголовка опроса до пункта freshness."""
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("0. **Startup survey.**")
    return text[start:text.index("1. **Base-index freshness.**", start)]


def test_startup_survey_runs_before_preflight_checks():
    text = SKILL.read_text(encoding="utf-8")
    assert text.index("0. **Startup survey.**") < text.index("1. **Base-index freshness.**")
    assert text.index("0. **Startup survey.**") < text.index("3. **Warm the task corpus.**")


def test_survey_is_one_panel_with_three_questions():
    section = _survey_section()
    assert "AskUserQuestion" in section
    assert "one panel" in section
    # регистр важен: в промпте это заголовки вопросов с заглавной буквы
    assert "Brief model tier" in section
    assert "Interaction mode" in section
    assert "Execution strategy" in section


def test_survey_offers_three_interaction_modes_each_explained():
    section = _survey_section()
    for value in ("`normal`", "`auto`", "`full-auto`"):
        assert value in section, f"нет режима {value}"
    assert "explain what it means" in section   # пояснение обязательно у каждого значения


def test_survey_offers_four_execution_strategies():
    section = _survey_section()
    for value in ("`inline`", "`subagent`", "`lite`", "`auto`"):
        assert value in section, f"нет стратегии {value}"


def test_survey_defaults_are_fail_open():
    section = _survey_section()
    assert "never block" in section
    assert "`mid`" in section
    assert "defaults" in section
    assert "non-interactive" in section          # единственное исключение из показа панели


def test_auto_permission_mode_shortcut_removed():
    # Правило «в auto permission mode тир выбирается молча» удалено: панель
    # показывается всегда, кроме headless/non-interactive.
    text = SKILL.read_text(encoding="utf-8")
    assert "auto permission mode" not in text
    assert "1.5. **Choose the brief model" not in text


def test_brief_building_unit_points_at_the_survey():
    text = SKILL.read_text(encoding="utf-8")
    assert "chosen in Step 1.5" not in text
    assert "Step 0 startup survey" in text


def test_full_auto_suppresses_preflight_questions():
    text = SKILL.read_text(encoding="utf-8")
    assert "In `full-auto`, do not ask" in text
    assert "recommended option" in text
