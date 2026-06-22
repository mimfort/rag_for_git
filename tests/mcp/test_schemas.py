"""Тесты FindingIn/VerdictIn — мягкая коэрция (порт _finding_from_dict, PRI-144)."""
from __future__ import annotations

import pytest

from reviewer.mcp.schemas import FindingIn, FixIn, VerdictIn

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


def test_category_default():
    assert FindingIn.model_validate(BASE).category == "correctness"
    assert FindingIn.model_validate({**BASE, "category": "security"}).category == "security"


def test_verdict_in():
    v = VerdictIn.model_validate({"id": "f3", "is_real": False})
    assert v.id == "f3" and v.is_real is False
    with pytest.raises(Exception):
        VerdictIn.model_validate({"id": "f3"})   # is_real required
