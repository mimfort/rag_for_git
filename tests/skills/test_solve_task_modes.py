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
