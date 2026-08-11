"""Фильтр «наш баг / не наш», уровни и дедуп по сигнатуре (PRI-239).

Набор сбоев взят из самой задачи: канал обязан молчать на окружении, внешних сервисах,
коде пользователя и правах — иначе он станет шумом и его выключат.
"""
import pytest

from reviewer.bugreport.triage import FOREIGN_KINDS, OUR_KINDS, signature, triage


@pytest.mark.parametrize("kind", sorted(OUR_KINDS))
def test_tool_defects_are_reported(kind):
    assert triage(kind, severity="degraded").ours is True


@pytest.mark.parametrize("kind", sorted(FOREIGN_KINDS))
def test_foreign_problems_stay_silent(kind):
    verdict = triage(kind, severity="blocker")
    assert verdict.ours is False
    assert verdict.reason.startswith("не дефект инструмента")


def test_unknown_kind_stays_silent():
    assert triage("что-то странное").ours is False


def test_llm_behaviour_becomes_ours_when_the_skill_instruction_contradicts_the_server():
    verdict = triage("llm_behaviour", caused_by_skill_instruction=True)
    assert verdict.ours is True
    assert verdict.kind == "skill_contradiction"


def test_llm_behaviour_alone_stays_silent():
    assert triage("llm_behaviour").ours is False


@pytest.mark.parametrize(
    ("given", "expected"),
    [("blocker", "blocker"), ("degraded", "degraded"), ("contract", "contract"),
     ("cosmetic", "contract"), ("critical", "blocker"), (None, "degraded"), ("", "degraded")],
)
def test_severity_normalization(given, expected):
    assert triage("tool_exception", severity=given).severity == expected


def test_contract_level_is_deferred_and_blocking_levels_are_not():
    assert triage("contract_violation", severity="contract").defer is True
    assert triage("contract_violation", severity="blocker").defer is False
    assert triage("contract_violation", severity="degraded").defer is False


def test_signature_is_stable_across_wording_of_the_same_symptom():
    first = signature("tool_exception", "finish_task", "step 5", "KeyError")
    second = signature("Tool_Exception", "finish_task", "Step  5", "KeyError")
    assert first == second


def test_signature_separates_different_symptoms():
    assert signature("tool_exception", "finish_task") != signature("tool_exception", "sync_board")


def test_signature_carries_no_readable_payload():
    # Сигнатура уходит в публичный issue, поэтому она обязана быть дайджестом.
    value = signature("tool_exception", "finish_task", "step 5", "KeyError")
    assert "finish_task" not in value
    assert len(value) == 16
