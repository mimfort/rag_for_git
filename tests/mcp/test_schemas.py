"""Тесты FindingIn/VerdictIn — мягкая коэрция (порт _finding_from_dict, PRI-144)."""
from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from reviewer.mcp.schemas import FindingIn, FixIn, SubtaskIn, SubtasksIn, VerdictIn
from reviewer.tasks.subtasks import MAX_SUBTASKS

BASE = {"file": "a.py", "severity": "high", "message": "m", "line": 1, "code_quote": "x = 1"}


def test_confidence_coercion_and_clamp():
    # валидные проходят как есть
    assert FindingIn.model_validate({**BASE, "confidence": 0.9}).confidence == 0.9
    assert FindingIn.model_validate({**BASE, "confidence": 0.5}).confidence == 0.5
    # не оценено / None / мусор → 0.1
    assert FindingIn.model_validate(BASE).confidence == 0.1
    assert FindingIn.model_validate({**BASE, "confidence": None}).confidence == 0.1
    assert FindingIn.model_validate({**BASE, "confidence": "abc"}).confidence == 0.1
    # clamp в [0,1]
    assert FindingIn.model_validate({**BASE, "confidence": 1.5}).confidence == 1.0
    assert FindingIn.model_validate({**BASE, "confidence": -0.2}).confidence == 0.0


def test_severity_side_defaults():
    assert FindingIn.model_validate({**BASE, "severity": "urgent"}).severity == "medium"
    assert FindingIn.model_validate({**BASE, "severity": ["high"]}).severity == "medium"
    assert FindingIn.model_validate({**BASE, "side": "MIDDLE"}).side == "RIGHT"
    assert FindingIn.model_validate({**BASE, "side": "LEFT"}).side == "LEFT"


def test_line_coercion():
    assert FindingIn.model_validate({**BASE, "line": "42"}).line == 42
    assert FindingIn.model_validate({**BASE, "line": "abc"}).line is None
    assert FindingIn.model_validate({**BASE, "line": None}).line is None


def test_suggestion_and_codequote_nonstring_to_none():
    assert FindingIn.model_validate({**BASE, "suggestion": 42}).suggestion is None
    assert FindingIn.model_validate({**BASE, "suggestion": "do x"}).suggestion == "do x"
    assert FindingIn.model_validate({**BASE, "code_quote": 7}).code_quote is None


def test_fix_dropped_when_incomplete():
    ok = FindingIn.model_validate({**BASE, "fix": {"start_line": 1, "end_line": 2, "replacement": "z"}})
    assert ok.fix == FixIn(start_line=1, end_line=2, replacement="z")
    # нестроковый replacement → вся fix отбрасывается
    assert FindingIn.model_validate({**BASE, "fix": {"start_line": 1, "end_line": 2, "replacement": 9}}).fix is None
    # мусорный start_line → вся fix отбрасывается
    assert FindingIn.model_validate({**BASE, "fix": {"start_line": "x", "end_line": 2, "replacement": "z"}}).fix is None
    assert FindingIn.model_validate({**BASE, "fix": None}).fix is None


def test_file_required():
    with pytest.raises(Exception):
        FindingIn.model_validate({"severity": "high", "message": "no file"})


def test_file_rejects_empty_string():
    with pytest.raises(Exception):
        FindingIn.model_validate({**BASE, "file": ""})
    # пробел-непустой проходит (паритет со старым _finding_from_dict: falsy-проверка)
    assert FindingIn.model_validate({**BASE, "file": " "}).file == " "


def test_category_default():
    assert FindingIn.model_validate(BASE).category == "correctness"
    assert FindingIn.model_validate({**BASE, "category": "security"}).category == "security"


def test_verdict_in():
    v = VerdictIn.model_validate({"id": "f3", "is_real": False})
    assert v.id == "f3" and v.is_real is False
    with pytest.raises(Exception):
        VerdictIn.model_validate({"id": "f3"})   # is_real required


def test_verdict_in_reason_optional_defaults_none():
    from reviewer.mcp.schemas import VerdictIn
    v = VerdictIn.model_validate({"id": "f1", "is_real": False})
    assert v.reason is None


def test_verdict_in_reason_accepted():
    from reviewer.mcp.schemas import VerdictIn
    v = VerdictIn.model_validate(
        {"id": "f1", "is_real": False, "reason": "line does not exist"})
    assert v.reason == "line does not exist"


def test_verdict_in_reason_coercion_to_none():
    # пустая/whitespace-only/нестроковая причина → None (валидатор _reason —
    # единственное место коэрции: submit_verdicts фильтрует `if v.reason:`, а
    # "   " в Python truthy, поэтому без валидатора whitespace-причина осела бы).
    assert VerdictIn.model_validate({"id": "f1", "is_real": False, "reason": ""}).reason is None
    assert VerdictIn.model_validate({"id": "f1", "is_real": False, "reason": "   "}).reason is None
    assert VerdictIn.model_validate({"id": "f1", "is_real": False, "reason": 42}).reason is None


SUBTASK = {
    "title": " Дочерняя задача ",
    "problem": " Решить часть проблемы ",
    "steps": [" Первый шаг ", " ", "Второй шаг"],
    "criteria": [" Готово ", ""],
    "context": " Контекст ",
}


def test_subtask_in_strips_required_fields_lists_and_context():
    item = SubtaskIn.model_validate(SUBTASK)

    assert item.model_dump() == {
        "title": "Дочерняя задача",
        "problem": "Решить часть проблемы",
        "steps": ["Первый шаг", "Второй шаг"],
        "criteria": ["Готово"],
        "context": "Контекст",
    }
    assert SubtaskIn.model_validate({**SUBTASK, "context": "   "}).context is None


@pytest.mark.parametrize("field", ["title", "problem"])
def test_subtask_in_rejects_blank_required_text(field):
    with pytest.raises(ValidationError):
        SubtaskIn.model_validate({**SUBTASK, field: "   "})


@pytest.mark.parametrize("field", ["steps", "criteria"])
def test_subtask_in_drops_blank_list_members_but_requires_one(field):
    with pytest.raises(ValidationError):
        SubtaskIn.model_validate({**SUBTASK, field: ["", "  "]})


def test_subtask_in_forbids_extra_fields():
    with pytest.raises(ValidationError):
        SubtaskIn.model_validate({**SUBTASK, "unexpected": True})


def test_subtask_in_rejects_non_string_context_as_validation_error():
    with pytest.raises(ValidationError):
        SubtaskIn.model_validate({**SUBTASK, "context": 42})


def test_subtasks_in_enforces_one_to_twenty_items_and_publishes_max_items():
    adapter = TypeAdapter(SubtasksIn)
    valid = {**SUBTASK, "title": "Child"}

    assert len(adapter.validate_python([valid] * MAX_SUBTASKS)) == MAX_SUBTASKS
    with pytest.raises(ValidationError):
        adapter.validate_python([])
    with pytest.raises(ValidationError):
        adapter.validate_python([valid] * (MAX_SUBTASKS + 1))
    assert adapter.json_schema()["maxItems"] == MAX_SUBTASKS
