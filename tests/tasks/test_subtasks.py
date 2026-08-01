from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from typing import get_args

import pytest

from reviewer.tasks.subtasks import (
    MAX_SUBTASKS,
    SUBTASK_MARKER_RE,
    OperationStatus,
    SubtaskDraft,
    SubtaskPhase,
    marker_for,
    validate_subtask_request,
)

CHILD = {
    "title": "Дочерняя задача",
    "problem": "Нужно разделить работу",
    "steps": ["Первый шаг"],
    "criteria": ["Результат проверен"],
    "context": None,
}


def _validate(**overrides):
    values = {
        "parent_key": "PRI-224",
        "subtasks": [CHILD],
        "idempotency_key": "attempt-1",
        "board_type": "yougile",
        "project": "PRI",
        "provider_options": {"column": "todo"},
    }
    values.update(overrides)
    return validate_subtask_request(**values)


def test_contract_constants_and_literal_values():
    assert MAX_SUBTASKS == 20
    assert SUBTASK_MARKER_RE.pattern == r"reviewer-subtask:[0-9a-f]{64}"
    assert set(get_args(SubtaskPhase)) == {"pending", "in_flight", "created", "attached"}
    assert set(get_args(OperationStatus)) == {
        "running",
        "partial",
        "board_complete",
        "complete",
    }


def test_draft_payload_uses_json_compatible_lists_and_is_frozen():
    draft = SubtaskDraft(
        title="Задача",
        problem="Проблема",
        steps=("Шаг 1", "Шаг 2"),
        criteria=("Критерий",),
        context="Контекст",
    )

    assert draft.payload() == {
        "title": "Задача",
        "problem": "Проблема",
        "steps": ["Шаг 1", "Шаг 2"],
        "criteria": ["Критерий"],
        "context": "Контекст",
    }
    with pytest.raises(FrozenInstanceError):
        draft.title = "Другая задача"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("parent_key", ""),
        ("parent_key", " \t"),
        ("parent_key", None),
        ("parent_key", 224),
        ("idempotency_key", ""),
        ("idempotency_key", " \n"),
        ("idempotency_key", None),
        ("idempotency_key", 1),
    ],
)
def test_blank_or_non_string_identity_is_rejected(field, value):
    with pytest.raises(ValueError):
        _validate(**{field: value})


@pytest.mark.parametrize("field", ["title", "problem"])
@pytest.mark.parametrize("value", ["", "  ", None, 1])
def test_blank_or_non_string_required_child_text_is_rejected(field, value):
    child = {**CHILD, field: value}

    with pytest.raises(ValueError):
        _validate(subtasks=[child])


@pytest.mark.parametrize("field", ["steps", "criteria"])
@pytest.mark.parametrize("value", [[], ["", " \t"]])
def test_empty_effective_child_lists_are_rejected(field, value):
    child = {**CHILD, field: value}

    with pytest.raises(ValueError):
        _validate(subtasks=[child])


@pytest.mark.parametrize("count", [0, 21])
def test_child_count_outside_limits_is_rejected(count):
    with pytest.raises(ValueError):
        _validate(subtasks=[CHILD] * count)


def test_maximum_child_count_is_accepted():
    request = _validate(subtasks=[CHILD] * MAX_SUBTASKS)

    assert len(request.subtasks) == MAX_SUBTASKS


def test_child_must_be_a_dict():
    with pytest.raises(TypeError):
        _validate(subtasks=["not-a-dict"])


def test_child_values_are_normalized_into_tuples():
    child = {
        **CHILD,
        "title": "  Задача  ",
        "problem": "  Проблема  ",
        "steps": ["  Шаг  ", "", " \t"],
        "criteria": ["  Критерий  ", " "],
        "context": "  Контекст  ",
    }

    request = _validate(subtasks=[child])

    assert request.subtasks == (
        SubtaskDraft(
            title="Задача",
            problem="Проблема",
            steps=("Шаг",),
            criteria=("Критерий",),
            context="Контекст",
        ),
    )


@pytest.mark.parametrize("context", [1, [], {}])
def test_non_string_context_is_rejected(context):
    with pytest.raises(TypeError):
        _validate(subtasks=[{**CHILD, "context": context}])


@pytest.mark.parametrize("context", [None, "", " \t\n"])
def test_blank_context_is_normalized_to_none(context):
    request = _validate(subtasks=[{**CHILD, "context": context}])

    assert request.subtasks[0].context is None


def test_canonical_option_key_order_produces_same_request_hash():
    first = _validate(provider_options={"column": "todo", "nested": {"b": 2, "a": 1}})
    second = _validate(provider_options={"nested": {"a": 1, "b": 2}, "column": "todo"})

    assert first.request_hash == second.request_hash


def test_child_reordering_changes_request_hash():
    second_child = {**CHILD, "title": "Вторая задача"}

    forward = _validate(subtasks=[CHILD, second_child])
    reverse = _validate(subtasks=[second_child, CHILD])

    assert forward.request_hash != reverse.request_hash


def test_request_hash_matches_canonical_normalized_payload():
    request = _validate()
    canonical = json.dumps(
        {
            "parent_key": "PRI-224",
            "subtasks": [request.subtasks[0].payload()],
            "idempotency_key": "attempt-1",
            "board_type": "yougile",
            "project": "PRI",
            "provider_options": {"column": "todo"},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )

    assert request.request_hash == hashlib.sha256(canonical.encode()).hexdigest()


def test_provider_options_are_copied_not_aliased():
    options = {"column": "todo"}

    request = _validate(provider_options=options)
    options["column"] = "done"

    assert request.provider_options == {"column": "todo"}
    with pytest.raises(FrozenInstanceError):
        request.request_hash = "another-hash"


def test_non_finite_option_is_rejected_by_canonical_json():
    with pytest.raises(ValueError):
        _validate(provider_options={"weight": float("nan")})


def test_marker_is_stable_lowercase_and_index_specific():
    draft = _validate().subtasks[0]
    child_json = json.dumps(
        draft.payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    child_hash = hashlib.sha256(child_json.encode()).hexdigest()
    marker_components = (
        "yougile",
        "task-42",
        "attempt-1",
        "0",
        child_hash,
    )
    marker_payload = "\0".join(marker_components)
    expected = "reviewer-subtask:" + hashlib.sha256(marker_payload.encode()).hexdigest()

    first = marker_for("yougile", "task-42", "attempt-1", 0, draft)
    repeated = marker_for("yougile", "task-42", "attempt-1", 0, draft)
    another_index = marker_for("yougile", "task-42", "attempt-1", 1, draft)

    assert first == repeated == expected
    assert SUBTASK_MARKER_RE.fullmatch(first)
    assert first.removeprefix("reviewer-subtask:") == first.removeprefix(
        "reviewer-subtask:"
    ).lower()
    assert len(first.removeprefix("reviewer-subtask:")) == 64
    assert another_index != first


@pytest.mark.parametrize(
    ("board_type", "parent_task_id", "idempotency_key"),
    [
        ("board\0type", "parent", "key"),
        ("board", "parent\0id", "key"),
        ("board", "parent", "key\0part"),
    ],
)
def test_marker_rejects_nul_in_input_components(
    board_type,
    parent_task_id,
    idempotency_key,
):
    draft = _validate().subtasks[0]

    with pytest.raises(ValueError):
        marker_for(board_type, parent_task_id, idempotency_key, 0, draft)
