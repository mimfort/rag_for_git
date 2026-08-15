"""Guardrail: стартовый опрос solve-task и профиль исполнения lite (PRI-243).

Тесты маркерные: пинят стабильные якоря спеки, а не формулировки, чтобы правка
промпта не удалила требование молча.
"""
import re
from pathlib import Path

from .test_assembled_prompts import assemble

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "plugin" / "skills" / "_profiles" / "execution-lite.md"


def _solve() -> str:
    return assemble("solve-task/SKILL.md")


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
    text = _solve()
    start = text.index("0. **Startup survey.**")
    return text[start:text.index("1. **Base-index freshness.**", start)]


def test_startup_survey_runs_before_preflight_checks():
    text = _solve()
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


def test_survey_defaults_pin_the_whole_triple():
    # Голая проверка вхождения `mid` была бы вакуумной: `normal` и `subagent`
    # уже встречаются в срезе как значения опций. Пиним строку дефолтов целиком
    # (regex допускает перенос строки между "tier" и `mid`, как в исходнике).
    section = _survey_section()
    assert re.search(
        r"tier\s*`mid`,\s*mode\s*`normal`,\s*strategy\s*`subagent`", section
    ), "не найдена строка дефолтов целиком: tier mid, mode normal, strategy subagent"


def test_auto_permission_mode_shortcut_removed():
    # Правило «в auto permission mode тир выбирается молча» удалено: панель
    # показывается всегда, кроме headless/non-interactive.
    text = _solve()
    assert "auto permission mode" not in text
    assert "auto-permission mode" not in text
    assert "1.5. **Choose the brief model" not in text


def test_brief_building_unit_points_at_the_survey():
    text = _solve()
    assert "chosen in Step 1.5" not in text
    assert "Step 0 startup survey" in text


def test_full_auto_suppresses_preflight_questions():
    text = _solve()
    assert "In `full-auto`, do not ask" in text
    assert "recommended option" in text


def _run_state_section() -> str:
    """Вырезать подраздел персиста выбора — от его заголовка до хендоффа."""
    text = _solve()
    start = text.index("**Persist the run state")
    return text[start:text.index("5. **Hand off to development.**", start)]


def _brief_persist_section() -> str:
    """Вырезать подраздел персиста брифа — он не должен нести режим."""
    text = _solve()
    start = text.index("**Persist the brief (survivability).**")
    return text[start:text.index("**Persist the run state", start)]


def _handoff_section() -> str:
    text = _solve()
    start = text.index("5. **Hand off to development.**")
    return text[start:text.index("## Failure handling", start)]


def test_run_state_persist_is_orchestrator_owned():
    # Critical (находка 1): Steps 2-4 могут диспатчиться сабагенту, у которого
    # нет опроса/предполёта/абсолютного пути плагина — персист файла прогона
    # обязан быть явно закреплён за оркестратором, а не за этим сабагентом.
    section = _run_state_section()
    assert "orchestrator" in section


def test_run_state_lives_in_gitignored_dir():
    section = _run_state_section()
    assert ".superpowers/solve-task/" in section
    assert "<KEY>.md" in section
    assert "git-ignored" in section


def test_run_state_records_mode_strategy_and_profile_path():
    section = _run_state_section()
    assert "Режим:" in section
    assert "Стратегия:" in section
    assert "_profiles/execution-lite.md" in section
    assert "absolute" in section          # путь профиля пишется абсолютным


def test_decisions_section_only_in_full_auto():
    section = _run_state_section()
    assert "Решения, принятые за пользователя" in section
    assert "only in `full-auto`" in section


def test_mode_never_written_into_committed_artifacts():
    # Спека и план коммитятся: ни режим, ни перечень решений туда не пишутся.
    # Блок персиста брифа вправе *упоминать* full-auto в инструкциях исполнителю
    # (например, поведение existing-artifacts warn), но не вправе предписывать
    # запись значения режима как поля брифа.
    brief_section = _brief_persist_section()
    assert "Режим" not in brief_section
    run_state = _run_state_section()
    assert "never write the mode" in run_state
    assert "spec" in run_state and "plan" in run_state


def test_full_auto_confirmation_boundary_is_a_named_list():
    section = _handoff_section()
    assert "git push" in section
    assert "creating a PR" in section
    assert "board write" in section
    assert "sync_board` call in write mode" in section


def test_auto_strategy_rubric_has_observable_thresholds():
    section = _handoff_section()
    assert "> 8 tasks" in section
    assert "> 10" in section
    assert "≤ 3 tasks" in section
    assert "first match wins" in section   # правила упорядочены, ветка ровно одна


def test_auto_rubric_names_risk_signals():
    section = _handoff_section()
    for signal in ("schema migration", "MCP tool", "credentials", "irreversible"):
        assert signal in section, f"нет рискового признака {signal}"


def test_handoff_passes_mode_as_user_instruction():
    section = _handoff_section()
    assert "the user's explicit instruction" in section
    assert "not a request to bypass" in section


def test_handoff_requires_task_right_sizing():
    section = _handoff_section()
    assert "Task Right-Sizing" in section


def test_handoff_carries_run_state_path_forward():
    section = _handoff_section()
    assert ".superpowers/solve-task/" in section
    assert "re-read" in section
