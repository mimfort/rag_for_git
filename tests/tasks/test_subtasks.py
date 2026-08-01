from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from typing import get_args

import pytest

import reviewer.tasks.subtasks as subtasks_module
from reviewer.tasks.boards.base import (
    NativeSubtaskIdentity,
    RawTask,
    ReconciledNativeSubtask,
)
from reviewer.tasks.subtask_store import (
    LedgerUnavailableError,
    OperationConflictError,
    SubtaskOperation,
)
from reviewer.tasks.subtasks import (
    MAX_SUBTASKS,
    SUBTASK_MARKER_RE,
    OperationStatus,
    SubtaskBatchResult,
    SubtaskChildResult,
    SubtaskDraft,
    SubtaskPhase,
    SubtaskPreflight,
    SubtaskService,
    WriteThroughResult,
    marker_for,
    merge_subtask_ids,
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


def _input_hash(draft):
    canonical = json.dumps(
        draft.payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _request_payload_hash(payload):
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class MemoryStore:
    def __init__(
        self,
        operation: SubtaskOperation | None = None,
        *,
        events=None,
        busy=False,
        dead=False,
        stale=False,
        insert_race: SubtaskOperation | None = None,
    ):
        self.operations = {}
        if operation is not None:
            self.operations[operation.idempotency_key] = deepcopy(operation)
        self.events = events if events is not None else []
        self.busy = busy
        self.dead = dead
        self.stale = stale
        self.insert_race = insert_race
        self.inserted_snapshots = []
        self.checkpoint_snapshots = []
        self.checkpoint_history = []
        self.lock_active = False

    def load(self, idempotency_key):
        operation = self.operations.get(idempotency_key)
        return deepcopy(operation) if operation is not None else None

    def insert(self, operation):
        if self.insert_race is not None:
            concurrent = deepcopy(self.insert_race)
            self.insert_race = None
            self.operations[concurrent.idempotency_key] = concurrent
            raise OperationConflictError("concurrent insert")
        if operation.idempotency_key in self.operations:
            raise RuntimeError("duplicate operation")
        snapshot = deepcopy(operation)
        self.inserted_snapshots.append(snapshot)
        self.operations[operation.idempotency_key] = snapshot
        return deepcopy(snapshot)

    def checkpoint(self, operation, *, expected_revision):
        if self.stale:
            raise OperationConflictError("stale revision")
        current = self.operations[operation.idempotency_key]
        if current.revision != expected_revision:
            raise OperationConflictError("stale revision")
        snapshot = deepcopy(operation)
        self.checkpoint_snapshots.append(snapshot)
        self.checkpoint_history.append(
            (
                expected_revision,
                snapshot.status,
                tuple(
                    (item["index"], item["phase"], item["input_hash"])
                    for item in snapshot.state["items"]
                ),
            )
        )
        phase = snapshot.state["items"][0]["phase"]
        self.events.append(("checkpoint", phase, expected_revision))
        state = deepcopy(snapshot.state)
        state["revision"] = expected_revision + 1
        persisted = replace(snapshot, state=state)
        self.operations[operation.idempotency_key] = persisted
        return deepcopy(persisted)

    @contextmanager
    def try_parent_lock(self, board_type, parent_task_id):
        self.events.append(("lock", board_type, parent_task_id))
        if self.busy:
            yield None
            return
        store = self

        class Lock:
            def ensure_alive(self):
                store.events.append(("ensure_alive",))
                if store.dead:
                    raise LedgerUnavailableError("dead lock")

        self.lock_active = True
        try:
            yield Lock()
        finally:
            self.lock_active = False


class FakeProvider:
    def __init__(
        self,
        parent,
        *,
        events=None,
        reconciled=None,
        create_effects=None,
        fetch_effects=None,
        replace_effects=None,
    ):
        self.parent = parent
        self.events = events if events is not None else []
        self.reconciled = list(reconciled or [])
        self.create_effects = list(create_effects or [])
        self.fetch_effects = list(fetch_effects or [])
        self.replace_effects = list(replace_effects or [])
        self.fetch_calls = []
        self.reconcile_calls = []
        self.create_calls = []
        self.replace_calls = []

    def fetch_one(self, key):
        self.fetch_calls.append(key)
        if self.fetch_effects:
            effect = self.fetch_effects.pop(0)
            if callable(effect):
                return effect(key)
            if isinstance(effect, BaseException):
                raise effect
            return effect
        return self.parent

    def reconcile_native_subtasks(self, source_board_id, markers):
        self.reconcile_calls.append((source_board_id, markers))
        return list(self.reconciled)

    def create_native_subtask(
        self,
        doc_md,
        *,
        title,
        source_column_id,
        marker,
    ):
        call = {
            "doc_md": doc_md,
            "title": title,
            "source_column_id": source_column_id,
            "marker": marker,
        }
        self.create_calls.append(call)
        self.events.append(("post", marker))
        effect = self.create_effects.pop(0)
        if callable(effect):
            return effect(marker)
        if isinstance(effect, BaseException):
            raise effect
        return effect

    def replace_native_subtasks(self, parent_task_id, subtask_ids):
        snapshot = list(subtask_ids)
        self.replace_calls.append((parent_task_id, snapshot))
        self.events.append(("put", parent_task_id, tuple(snapshot)))
        if self.replace_effects:
            effect = self.replace_effects.pop(0)
            if callable(effect):
                return effect(parent_task_id, snapshot)
            if isinstance(effect, BaseException):
                raise effect
        return None


def _parent(**overrides):
    values = {
        "key": "PRI-224",
        "project_code": "PRI-224",
        "title": "Родитель",
        "description": "",
        "status": "Новые",
        "subtask_ids": [],
        "timestamp": 1,
        "board_id": "parent-uuid",
        "provider_data": {
            "source_board_id": "board-uuid",
            "source_column_id": "column-uuid",
        },
    }
    values.update(overrides)
    return RawTask(**values)


def _run(
    service,
    request,
    provider,
    *,
    operation=None,
    sanitize=None,
    write_calls=None,
    write_effects=None,
):
    write_calls = write_calls if write_calls is not None else []
    write_effects = list(write_effects or [])

    def write_through(parent, identities):
        write_calls.append((parent, identities))
        if write_effects:
            effect = write_effects.pop(0)
            if callable(effect):
                return effect(parent, identities)
            if isinstance(effect, BaseException):
                raise effect
            return effect
        return WriteThroughResult(True)

    return service.run(
        request,
        operation=operation,
        provider=provider,
        board_type="yougile",
        write_through=write_through,
        sanitize=sanitize or (lambda value: str(value)),
    )


def _operation(request=None, *, status="running", request_hash=None, state=None):
    request = request or _validate()
    return SubtaskOperation(
        idempotency_key=request.idempotency_key,
        board_type="yougile",
        parent_input_key=request.parent_key,
        parent_task_id="parent-uuid",
        source_board_id="board-uuid",
        source_column_id="column-uuid",
        request_hash=request_hash or request.request_hash,
        request_payload={
            "parent_key": request.parent_key,
            "subtasks": [draft.payload() for draft in request.subtasks],
            "idempotency_key": request.idempotency_key,
            "board_type": request.board_type,
            "project": request.project,
            "provider_options": request.provider_options,
        },
        state=state or {"revision": 0, "items": []},
        status=status,
    )


def _pending_operation(request=None):
    request = request or _validate()
    items = []
    for index, draft in enumerate(request.subtasks):
        items.append(
            {
                "index": index,
                "title": draft.title,
                "input_hash": _input_hash(draft),
                "marker": marker_for(
                    "yougile",
                    "parent-uuid",
                    request.idempotency_key,
                    index,
                    draft,
                ),
                "phase": "pending",
                "key": None,
                "aliases": [],
                "board_id": None,
                "url": None,
                "manual_required": False,
                "warnings": [],
            }
        )
    return _operation(request, state={"revision": 0, "items": items})


def _in_flight_operation(request=None, *, revision=0, manual_required=False, warnings=None):
    request = request or _validate()
    operation = _pending_operation(request)
    operation.state["revision"] = revision
    operation.state["items"][0].update(
        {
            "phase": "in_flight",
            "manual_required": manual_required,
            "warnings": list(warnings or []),
        }
    )
    return replace(operation, status="partial")


def _attached_operation(request=None):
    request = request or _validate()
    operation = _in_flight_operation(request)
    item = operation.state["items"][0]
    item.update(
        {
            "phase": "attached",
            "key": "PRI-225",
            "aliases": ["TASK-2"],
            "board_id": "child-uuid",
            "url": "https://board/child-uuid",
        }
    )
    return replace(operation, status="complete")


def _malformed_operation(request, case):
    operation = _pending_operation(request)
    state = deepcopy(operation.state)
    payload = deepcopy(operation.request_payload)
    items = state["items"]

    def created(item, *, phase="created"):
        item.update(
            {
                "phase": phase,
                "key": "PRI-225",
                "aliases": [],
                "board_id": "child-uuid",
                "url": None,
            }
        )

    if case == "state_not_object":
        return replace(operation, state=[])
    if case == "request_payload_not_object":
        return replace(operation, request_payload=[])
    if case == "revision_negative":
        state["revision"] = -1
    elif case == "revision_bool":
        state["revision"] = True
    elif case == "items_not_list":
        state["items"] = {}
    elif case == "item_count":
        items.pop()
    elif case == "duplicate_indices":
        items[1]["index"] = 0
    elif case == "wrong_title":
        items[0]["title"] = "Другой заголовок"
    elif case == "wrong_input_hash":
        items[0]["input_hash"] = "f" * 64
    elif case == "wrong_marker":
        items[0]["marker"] = "reviewer-subtask:" + "f" * 64
    elif case == "invalid_phase":
        items[0]["phase"] = "unknown"
    elif case == "invalid_manual":
        items[0]["manual_required"] = "yes"
    elif case == "invalid_warnings":
        items[0]["warnings"] = [object()]
    elif case == "pending_identity":
        items[0]["board_id"] = "possible-side-effect"
    elif case.startswith("in_flight_"):
        items[0]["phase"] = "in_flight"
        operation = replace(operation, status="partial")
        if case == "in_flight_key_only":
            items[0]["key"] = "PRI-225"
        elif case == "in_flight_board_only":
            items[0]["board_id"] = "child-uuid"
        elif case == "in_flight_aliases_only":
            items[0]["aliases"] = ["TASK-2"]
        elif case == "in_flight_url_only":
            items[0]["url"] = "https://board/child-uuid"
        elif case == "in_flight_complete_identity":
            items[0].update(
                {
                    "key": "PRI-225",
                    "board_id": "child-uuid",
                    "aliases": ["TASK-2"],
                    "url": "https://board/child-uuid",
                }
            )
        else:
            raise AssertionError(f"unknown in-flight case: {case}")
    elif case == "created_without_board_id":
        created(items[0])
        items[0]["board_id"] = ""
        operation = replace(operation, status="partial")
    elif case == "created_manual_required":
        created(items[0])
        items[0]["manual_required"] = True
        operation = replace(operation, status="partial")
    elif case == "attached_without_key":
        created(items[0], phase="attached")
        items[0]["key"] = None
        operation = replace(operation, status="partial")
    elif case == "complete_attached_manual_required":
        for item in items:
            created(item, phase="attached")
        items[0]["manual_required"] = True
        operation = replace(operation, status="complete")
    elif case == "invalid_aliases":
        created(items[0])
        items[0]["aliases"] = [1]
        operation = replace(operation, status="partial")
    elif case == "blank_alias":
        created(items[0])
        items[0]["aliases"] = ["  "]
        operation = replace(operation, status="partial")
    elif case == "invalid_url":
        created(items[0])
        items[0]["url"] = 42
        operation = replace(operation, status="partial")
    elif case == "invalid_status":
        operation = replace(operation, status="mystery")
    elif case == "running_in_flight":
        items[0]["phase"] = "in_flight"
    elif case == "complete_pending":
        operation = replace(operation, status="complete")
    elif case == "board_complete_created":
        for item in items:
            created(item)
        operation = replace(operation, status="board_complete")
    elif case == "request_subtasks_not_list":
        payload["subtasks"] = {}
    elif case == "request_hash_mismatch":
        operation = replace(operation, request_hash="f" * 64)
    elif case == "board_type_mismatch":
        payload["board_type"] = "other"
        operation = replace(operation, request_hash=_request_payload_hash(payload))
    else:
        raise AssertionError(f"unknown malformed case: {case}")
    return replace(operation, state=state, request_payload=payload)


def test_contract_constants_and_literal_values():
    assert MAX_SUBTASKS == 20
    assert SUBTASK_MARKER_RE.pattern == r"reviewer-subtask:[0-9a-f]{64}(?![0-9A-Fa-f])"
    assert set(get_args(SubtaskPhase)) == {"pending", "in_flight", "created", "attached"}
    assert set(get_args(OperationStatus)) == {
        "running",
        "partial",
        "board_complete",
        "complete",
    }


def test_merge_subtask_ids_preserves_first_seen_order_and_removes_empty_duplicates():
    assert merge_subtask_ids(
        ["old", "", "child-1", "old"],
        ["child-0", "child-1", "", "child-2", "child-0"],
    ) == ["old", "child-1", "child-0", "child-2"]


def test_parent_attachment_puts_exact_existing_and_confirmed_union():
    request = _validate(subtasks=[CHILD, {**CHILD, "title": "Вторая задача"}])
    store = MemoryStore()
    provider = FakeProvider(
        None,
        create_effects=[
            NativeSubtaskIdentity("child-0", "PRI-225", "Первая задача"),
            NativeSubtaskIdentity("child-1", "PRI-226", "Вторая задача"),
        ],
        fetch_effects=[
            _parent(),
            _parent(subtask_ids=["old", "child-1"]),
            _parent(subtask_ids=["old", "child-1", "child-0"]),
        ],
    )
    write_calls = []

    result = _run(
        SubtaskService(store),
        request,
        provider,
        write_calls=write_calls,
    )

    assert provider.replace_calls == [
        ("parent-uuid", ["old", "child-1", "child-0"]),
    ]
    assert result.status == "ok"
    assert tuple(child.board_id for child in result.created) == ("child-0", "child-1")
    assert result.created == result.attached
    assert result.unattached == result.pending == ()
    assert tuple(identity.board_id for identity in write_calls[0][1]) == (
        "child-0",
        "child-1",
    )
    assert store.operations["attempt-1"].state["target_subtask_ids"] == [
        "old",
        "child-1",
        "child-0",
    ]
    assert store.operations["attempt-1"].status == "complete"


def test_exact_existing_union_skips_put_and_checkpoints_board_complete_before_complete():
    request = _validate()
    store = MemoryStore()
    parent_with_child = _parent(subtask_ids=["child-uuid"])
    provider = FakeProvider(
        None,
        create_effects=[NativeSubtaskIdentity("child-uuid", "PRI-225", "Child")],
        fetch_effects=[_parent(), parent_with_child],
    )
    write_calls = []

    result = _run(
        SubtaskService(store),
        request,
        provider,
        write_calls=write_calls,
    )

    assert provider.replace_calls == []
    assert [snapshot.status for snapshot in store.checkpoint_snapshots[-2:]] == [
        "board_complete",
        "complete",
    ]
    assert write_calls == [
        (
            parent_with_child,
            (
                NativeSubtaskIdentity(
                    board_id="child-uuid",
                    key="PRI-225",
                    title="Дочерняя задача",
                ),
            ),
        )
    ]
    assert result.status == "ok"
    assert result.reindexed is True


def test_successful_put_without_verified_board_state_does_not_claim_attachment():
    request = _validate()
    store = MemoryStore()
    provider = FakeProvider(
        None,
        create_effects=[NativeSubtaskIdentity("child-uuid", "PRI-225", "Child")],
        fetch_effects=[_parent(), _parent(), _parent()],
    )
    write_calls = []

    result = _run(
        SubtaskService(store),
        request,
        provider,
        write_calls=write_calls,
    )

    assert provider.replace_calls == [("parent-uuid", ["child-uuid"])]
    assert result.status == "partial"
    assert result.attached == ()
    assert result.unattached == result.created
    assert store.operations["attempt-1"].state["items"][0]["phase"] == "created"
    assert write_calls == []


def test_partial_target_verification_reconciles_each_child_bucket():
    request = _validate(subtasks=[CHILD, {**CHILD, "title": "Вторая задача"}])
    store = MemoryStore()
    provider = FakeProvider(
        None,
        create_effects=[
            NativeSubtaskIdentity("child-0", "PRI-225", "Первая"),
            NativeSubtaskIdentity("child-1", "PRI-226", "Вторая"),
        ],
        fetch_effects=[
            _parent(),
            _parent(),
            _parent(subtask_ids=["child-0"]),
        ],
    )

    result = _run(SubtaskService(store), request, provider)

    assert tuple(child.index for child in result.created) == (0, 1)
    assert tuple(child.index for child in result.attached) == (0,)
    assert tuple(child.index for child in result.unattached) == (1,)
    assert result.pending == ()
    assert [item["phase"] for item in store.operations["attempt-1"].state["items"]] == [
        "attached",
        "created",
    ]


def test_missing_preexisting_target_id_keeps_verified_child_nonterminal():
    request = _validate()
    store = MemoryStore()
    provider = FakeProvider(
        None,
        create_effects=[NativeSubtaskIdentity("child-uuid", "PRI-225", "Child")],
        fetch_effects=[
            _parent(),
            _parent(subtask_ids=["old"]),
            _parent(subtask_ids=["child-uuid"]),
        ],
    )
    write_calls = []

    result = _run(
        SubtaskService(store),
        request,
        provider,
        write_calls=write_calls,
    )

    persisted = store.operations["attempt-1"]
    assert provider.replace_calls == [("parent-uuid", ["old", "child-uuid"])]
    assert persisted.state["target_subtask_ids"] == ["old", "child-uuid"]
    assert persisted.state["items"][0]["phase"] == "attached"
    assert persisted.status == "partial"
    assert result.status == "partial"
    assert result.retryable is True
    assert result.attached == result.created
    assert result.unattached == ()
    assert write_calls == []


def test_retry_empty_refetch_reverts_attached_children_before_later_target_repair():
    request = _validate(subtasks=[CHILD, {**CHILD, "title": "Вторая задача"}])
    store = MemoryStore()
    provider = FakeProvider(
        None,
        create_effects=[
            NativeSubtaskIdentity("child-0", "PRI-225", "Первая"),
            NativeSubtaskIdentity("child-1", "PRI-226", "Вторая"),
        ],
        fetch_effects=[
            _parent(),
            _parent(subtask_ids=["old"]),
            _parent(subtask_ids=["child-0", "child-1"]),
            _parent(subtask_ids=["child-0", "child-1"]),
            _parent(),
            _parent(),
            _parent(subtask_ids=["old", "child-0", "child-1"]),
        ],
    )
    service = SubtaskService(store)

    first = _run(service, request, provider)
    second = _run(
        service,
        request,
        provider,
        operation=service.preflight(request).operation,
    )
    phases_after_empty_refetch = [
        item["phase"] for item in store.operations["attempt-1"].state["items"]
    ]
    third = _run(
        service,
        request,
        provider,
        operation=service.preflight(request).operation,
    )

    assert tuple(child.index for child in first.attached) == (0, 1)
    assert first.unattached == ()
    assert second.status == "partial"
    assert second.attached == ()
    assert tuple(child.index for child in second.unattached) == (0, 1)
    assert phases_after_empty_refetch == ["created", "created"]
    assert provider.replace_calls == [
        ("parent-uuid", ["old", "child-0", "child-1"]),
        ("parent-uuid", ["old", "child-0", "child-1"]),
        ("parent-uuid", ["old", "child-0", "child-1"]),
    ]
    assert len(provider.create_calls) == 2
    assert store.operations["attempt-1"].state["target_subtask_ids"] == [
        "old",
        "child-0",
        "child-1",
    ]
    assert third.status == "ok"
    assert third.created == third.attached


def test_retry_restores_durable_target_before_current_extras_without_recreating_child():
    request = _validate()
    store = MemoryStore()
    provider = FakeProvider(
        None,
        create_effects=[NativeSubtaskIdentity("child-uuid", "PRI-225", "Child")],
        fetch_effects=[
            _parent(),
            _parent(subtask_ids=["old"]),
            _parent(subtask_ids=["child-uuid"]),
            _parent(subtask_ids=["child-uuid", "concurrent"]),
            _parent(subtask_ids=["old", "child-uuid", "concurrent"]),
        ],
    )
    service = SubtaskService(store)

    first = _run(service, request, provider)
    second = _run(
        service,
        request,
        provider,
        operation=service.preflight(request).operation,
    )

    assert first.status == "partial"
    assert provider.create_calls and len(provider.create_calls) == 1
    assert provider.replace_calls == [
        ("parent-uuid", ["old", "child-uuid"]),
        ("parent-uuid", ["old", "child-uuid", "concurrent"]),
    ]
    assert store.operations["attempt-1"].state["target_subtask_ids"] == [
        "old",
        "child-uuid",
        "concurrent",
    ]
    assert second.status == "ok"
    assert second.created == second.attached


@pytest.mark.parametrize(
    "malformed_target",
    [None, [], [""], ["old", "old"], ["  "]],
)
def test_malformed_persisted_target_fails_closed_without_provider_or_callback(
    malformed_target,
):
    request = _validate()
    operation = _attached_operation(request)
    operation.state["target_subtask_ids"] = malformed_target
    store = MemoryStore(operation)
    provider = FakeProvider(_parent())
    write_calls = []

    with pytest.raises(LedgerUnavailableError, match="target_subtask_ids"):
        _run(
            SubtaskService(store),
            request,
            provider,
            operation=operation,
            write_calls=write_calls,
        )

    assert provider.fetch_calls == []
    assert provider.reconcile_calls == []
    assert provider.create_calls == []
    assert provider.replace_calls == []
    assert write_calls == []


def test_target_snapshot_is_checkpointed_before_put_and_remains_immutable():
    request = _validate()
    events = []
    store = MemoryStore(events=events)
    provider = FakeProvider(
        None,
        events=events,
        create_effects=[NativeSubtaskIdentity("child-uuid", "PRI-225", "Child")],
        fetch_effects=[
            _parent(),
            _parent(subtask_ids=["old"]),
            _parent(subtask_ids=["old", "child-uuid"]),
        ],
    )

    result = _run(SubtaskService(store), request, provider)

    target_snapshots = [
        snapshot
        for snapshot in store.checkpoint_snapshots
        if snapshot.state.get("target_subtask_ids") == ["old", "child-uuid"]
    ]
    put_index = events.index(("put", "parent-uuid", ("old", "child-uuid")))
    assert target_snapshots
    assert events[put_index - 2][0] == "checkpoint"
    assert events[put_index - 1] == ("ensure_alive",)
    target_snapshots[0].state["target_subtask_ids"].append("mutated")
    assert store.operations["attempt-1"].state["target_subtask_ids"] == [
        "old",
        "child-uuid",
    ]
    assert result.status == "ok"


def test_confirmed_child_attaches_while_sibling_remains_manual_pending():
    request = _validate(subtasks=[CHILD, {**CHILD, "title": "Вторая задача"}])
    store = MemoryStore()
    provider = FakeProvider(
        None,
        create_effects=[
            RuntimeError("token=SECRET"),
            NativeSubtaskIdentity("child-1", "PRI-226", "Вторая"),
        ],
        fetch_effects=[
            _parent(),
            _parent(),
            _parent(subtask_ids=["child-1"]),
        ],
    )

    result = _run(
        SubtaskService(store),
        request,
        provider,
        sanitize=lambda _value: "safe create failure",
    )

    assert provider.replace_calls == [("parent-uuid", ["child-1"])]
    assert tuple(child.index for child in result.attached) == (1,)
    assert tuple(child.index for child in result.pending) == (0,)
    assert result.pending[0].manual_required is True
    assert [item["phase"] for item in store.operations["attempt-1"].state["items"]] == [
        "in_flight",
        "attached",
    ]


def test_lock_health_is_checked_immediately_before_parent_put():
    request = _validate()
    events = []
    store = MemoryStore(events=events)
    provider = FakeProvider(
        None,
        events=events,
        create_effects=[NativeSubtaskIdentity("child-uuid", "PRI-225", "Child")],
        fetch_effects=[_parent(), _parent(), _parent()],
    )

    _run(SubtaskService(store), request, provider)

    put_index = events.index(("put", "parent-uuid", ("child-uuid",)))
    assert events[put_index - 1] == ("ensure_alive",)


@pytest.mark.parametrize("failure_point", ["reread", "put", "verification"])
def test_attachment_transport_failure_is_retryable_without_losing_child_identity(
    failure_point,
):
    request = _validate()
    secret_error = RuntimeError("password=VERY_SECRET")
    fetch_effects = [_parent()]
    replace_effects = []
    if failure_point == "reread":
        fetch_effects.append(secret_error)
    elif failure_point == "put":
        fetch_effects.append(_parent())
        replace_effects.append(secret_error)
    else:
        fetch_effects.extend([_parent(), secret_error])
    store = MemoryStore()
    provider = FakeProvider(
        None,
        create_effects=[NativeSubtaskIdentity("child-uuid", "PRI-225", "Child")],
        fetch_effects=fetch_effects,
        replace_effects=replace_effects,
    )

    result = _run(
        SubtaskService(store),
        request,
        provider,
        sanitize=lambda _value: "safe attachment failure",
    )

    persisted = store.operations["attempt-1"]
    assert result.status == "partial"
    assert result.retryable is True
    assert result.created[0].board_id == "child-uuid"
    assert result.unattached == result.created
    assert result.warnings[-1] == "safe attachment failure"
    assert persisted.state["items"][0]["board_id"] == "child-uuid"
    assert persisted.state["items"][0]["phase"] == "created"
    assert "VERY_SECRET" not in json.dumps(persisted.state)
    assert "VERY_SECRET" not in json.dumps(result.payload())


def test_write_through_false_stays_board_complete_and_replay_retries_only_callback():
    request = _validate()
    store = MemoryStore()
    provider = FakeProvider(
        None,
        create_effects=[NativeSubtaskIdentity("child-uuid", "PRI-225", "Child")],
        fetch_effects=[
            _parent(),
            _parent(subtask_ids=["child-uuid"]),
            _parent(subtask_ids=["child-uuid"], timestamp=2),
        ],
    )
    service = SubtaskService(store)
    write_calls = []

    first = _run(
        service,
        request,
        provider,
        write_calls=write_calls,
        write_effects=[WriteThroughResult(False, ("index unavailable",))],
    )
    persisted_after_first = deepcopy(store.operations["attempt-1"])
    calls_after_first = (
        len(provider.reconcile_calls),
        len(provider.create_calls),
        len(provider.replace_calls),
    )
    second = _run(
        service,
        request,
        provider,
        operation=service.preflight(request).operation,
        write_calls=write_calls,
    )

    assert first.status == "partial"
    assert first.category == "reindex_pending"
    assert first.retryable is True
    assert first.reindexed is False
    assert persisted_after_first.status == "board_complete"
    assert persisted_after_first.state["reindexed"] is False
    assert store.checkpoint_snapshots[-2].status == "board_complete"
    assert calls_after_first == (0, 1, 0)
    assert (
        len(provider.reconcile_calls),
        len(provider.create_calls),
        len(provider.replace_calls),
    ) == calls_after_first
    assert provider.fetch_calls == ["PRI-224", "parent-uuid", "parent-uuid"]
    assert len(write_calls) == 2
    assert second.status == "ok"
    assert second.reindexed is True
    assert store.operations["attempt-1"].status == "complete"


def test_write_through_exception_is_sanitized_and_persists_no_secret():
    request = _validate()
    store = MemoryStore()
    provider = FakeProvider(
        None,
        create_effects=[NativeSubtaskIdentity("child-uuid", "PRI-225", "Child")],
        fetch_effects=[_parent(), _parent(subtask_ids=["child-uuid"])],
    )

    result = _run(
        SubtaskService(store),
        request,
        provider,
        write_effects=[RuntimeError("token=VERY_SECRET")],
        sanitize=lambda _value: "safe callback failure",
    )

    persisted = store.operations["attempt-1"]
    assert persisted.status == "board_complete"
    assert result.status == "partial"
    assert result.category == "reindex_pending"
    assert result.warnings[-1] == "safe callback failure"
    assert "VERY_SECRET" not in json.dumps(persisted.state)
    assert "VERY_SECRET" not in json.dumps(result.payload())


def test_callback_success_runs_under_parent_lock_after_board_complete_checkpoint():
    request = _validate()
    events = []
    store = MemoryStore(events=events)
    provider = FakeProvider(
        None,
        events=events,
        create_effects=[NativeSubtaskIdentity("child-uuid", "PRI-225", "Child")],
        fetch_effects=[_parent(), _parent(subtask_ids=["child-uuid"])],
    )

    def successful_callback(_parent_task, _identities):
        assert store.lock_active is True
        assert store.operations["attempt-1"].status == "board_complete"
        return WriteThroughResult(True, ("safe callback warning",))

    result = _run(
        SubtaskService(store),
        request,
        provider,
        write_effects=[successful_callback],
    )

    assert [snapshot.status for snapshot in store.checkpoint_snapshots[-2:]] == [
        "board_complete",
        "complete",
    ]
    assert result.status == "ok"
    assert result.warnings[-1] == "safe callback warning"
    assert result.reindexed is True


def test_board_complete_replay_reads_current_parent_and_calls_only_write_through():
    request = _validate()
    operation = replace(_attached_operation(request), status="board_complete")
    store = MemoryStore(operation)
    current_parent = _parent(subtask_ids=["child-uuid"], timestamp=5)
    provider = FakeProvider(None, fetch_effects=[current_parent])
    write_calls = []

    result = _run(
        SubtaskService(store),
        request,
        provider,
        operation=operation,
        write_calls=write_calls,
    )

    assert provider.fetch_calls == ["parent-uuid"]
    assert provider.reconcile_calls == []
    assert provider.create_calls == []
    assert provider.replace_calls == []
    assert write_calls[0][0] == current_parent
    assert tuple(identity.board_id for identity in write_calls[0][1]) == ("child-uuid",)
    assert result.status == "ok"
    assert result.resumed is True
    assert result.created == result.attached


def test_service_result_contracts_are_frozen_and_have_safe_payloads():
    child = SubtaskChildResult(
        index=0,
        title="Дочерняя задача",
        key="PRI-225",
        aliases=("TASK-2",),
        board_id="child-uuid",
        url="https://board/child-uuid",
        phase="created",
    )
    result = SubtaskBatchResult(
        status="ok",
        board_type="yougile",
        parent_key="PRI-224",
        idempotency_key="attempt-1",
        resumed=False,
        created=(child,),
    )

    assert result.payload() == {
        "status": "ok",
        "board_type": "yougile",
        "parent_key": "PRI-224",
        "idempotency_key": "attempt-1",
        "resumed": False,
        "created": (
            {
                "index": 0,
                "title": "Дочерняя задача",
                "key": "PRI-225",
                "aliases": ("TASK-2",),
                "board_id": "child-uuid",
                "url": "https://board/child-uuid",
                "phase": "created",
                "manual_required": False,
            },
        ),
        "attached": (),
        "unattached": (),
        "pending": (),
        "warnings": (),
        "reindexed": False,
        "category": None,
        "retryable": None,
    }
    assert WriteThroughResult(True) == WriteThroughResult(success=True, warnings=())
    assert SubtaskPreflight(operation=None, result=result).result is result
    with pytest.raises(FrozenInstanceError):
        child.phase = "pending"


def test_preflight_distinguishes_missing_conflict_incomplete_and_complete_operations():
    request = _validate()
    service = SubtaskService(MemoryStore())

    assert service.preflight(request) == SubtaskPreflight(None, None)

    conflicting = _pending_operation(_validate(parent_key="PRI-999"))
    conflict = SubtaskService(MemoryStore(conflicting)).preflight(request)
    assert conflict.operation == conflicting
    assert conflict.result is not None
    assert conflict.result.status == "error"
    assert conflict.result.resumed is True
    assert conflict.result.category == "conflict"
    assert conflict.result.retryable is False

    incomplete = _pending_operation(request)
    assert SubtaskService(MemoryStore(incomplete)).preflight(request) == SubtaskPreflight(
        incomplete,
        None,
    )

    complete = _operation(
        request,
        status="complete",
        state={
            "revision": 4,
            "items": [
                {
                    "index": 0,
                    "title": "Дочерняя задача",
                    "input_hash": _input_hash(request.subtasks[0]),
                    "marker": marker_for(
                        "yougile",
                        "parent-uuid",
                        request.idempotency_key,
                        0,
                        request.subtasks[0],
                    ),
                    "phase": "attached",
                    "key": "PRI-225",
                    "aliases": ["TASK-2"],
                    "board_id": "child-uuid",
                    "url": "https://board/child-uuid",
                    "manual_required": False,
                    "warnings": [],
                }
            ],
            "warnings": ["safe warning"],
            "reindexed": True,
        },
    )
    replay = SubtaskService(MemoryStore(complete)).preflight(request)

    assert replay.operation == complete
    assert replay.result is not None
    assert replay.result.status == "ok"
    assert replay.result.resumed is True
    assert replay.result.attached[0].key == "PRI-225"
    assert replay.result.created == replay.result.attached
    assert replay.result.unattached == ()
    assert replay.result.warnings == ("safe warning",)
    assert replay.result.reindexed is True


def test_fresh_run_checkpoints_before_post_and_persists_created_identity():
    request = _validate()
    events = []
    store = MemoryStore(events=events)
    identity = NativeSubtaskIdentity(
        board_id="child-uuid",
        key="PRI-225",
        title="Канонический заголовок доски",
        aliases=("TASK-2",),
        url="https://board/child-uuid",
        warnings=("safe provider warning",),
    )
    provider = FakeProvider(
        _parent(),
        events=events,
        create_effects=[identity],
    )
    write_calls = []

    result = _run(
        SubtaskService(store),
        request,
        provider,
        write_calls=write_calls,
    )

    marker = marker_for("yougile", "parent-uuid", "attempt-1", 0, request.subtasks[0])
    input_hash = _input_hash(request.subtasks[0])
    assert events[1:5] == [
        ("checkpoint", "in_flight", 0),
        ("ensure_alive",),
        ("post", marker),
        ("checkpoint", "created", 1),
    ]
    assert provider.create_calls == [
        {
            "doc_md": (
                "## Проблема\n\nНужно разделить работу\n\n"
                "## Что сделать\n\n1. Первый шаг\n\n"
                "## Критерии приёмки\n\n1. Результат проверен"
            ),
            "title": "Дочерняя задача",
            "source_column_id": "column-uuid",
            "marker": marker,
        }
    ]
    persisted = store.operations["attempt-1"]
    assert persisted.parent_task_id == "parent-uuid"
    assert persisted.source_board_id == "board-uuid"
    assert persisted.source_column_id == "column-uuid"
    assert persisted.request_hash == request.request_hash
    assert persisted.request_payload == {
        "parent_key": "PRI-224",
        "subtasks": [request.subtasks[0].payload()],
        "idempotency_key": "attempt-1",
        "board_type": "yougile",
        "project": "PRI",
        "provider_options": {"column": "todo"},
    }
    assert persisted.revision == 3
    assert persisted.status == "partial"
    assert persisted.state["target_subtask_ids"] == ["child-uuid"]
    assert persisted.state["items"][0] == {
        "index": 0,
        "title": "Дочерняя задача",
        "input_hash": input_hash,
        "marker": marker,
        "phase": "created",
        "key": "PRI-225",
        "aliases": ["TASK-2"],
        "board_id": "child-uuid",
        "url": "https://board/child-uuid",
        "manual_required": False,
        "warnings": ["safe provider warning"],
    }
    assert store.inserted_snapshots[0].state["revision"] == 0
    assert store.inserted_snapshots[0].state["items"][0]["phase"] == "pending"
    assert store.checkpoint_snapshots[0].state["items"][0]["phase"] == "in_flight"
    assert result.status == "partial"
    assert result.created == (
        SubtaskChildResult(
            index=0,
            title="Дочерняя задача",
            key="PRI-225",
            aliases=("TASK-2",),
            board_id="child-uuid",
            url="https://board/child-uuid",
            phase="created",
        ),
    )
    assert result.unattached == result.created
    assert result.attached == ()
    assert result.warnings == ("safe provider warning",)
    assert write_calls == []


def test_persisted_in_flight_item_reconciles_unique_identity_without_post():
    request = _validate()
    operation = _in_flight_operation(request, revision=3)
    marker = operation.state["items"][0]["marker"]
    identity = NativeSubtaskIdentity(
        board_id="child-uuid",
        key="PRI-225",
        title="Канонический заголовок доски",
        aliases=("TASK-2",),
        url="https://board/child-uuid",
    )
    provider = FakeProvider(
        _parent(),
        reconciled=[ReconciledNativeSubtask(marker, identity)],
    )
    store = MemoryStore(operation)

    result = _run(SubtaskService(store), request, provider, operation=operation)

    assert provider.reconcile_calls == [("board-uuid", frozenset({marker}))]
    assert provider.create_calls == []
    assert store.operations["attempt-1"].revision == 5
    assert store.operations["attempt-1"].state["items"][0]["board_id"] == "child-uuid"
    assert store.operations["attempt-1"].state["items"][0]["title"] == "Дочерняя задача"
    assert result.status == "partial"
    assert result.resumed is True
    assert result.created[0].key == "PRI-225"
    assert result.created[0].title == "Дочерняя задача"
    assert result.unattached == result.created


def test_unresolved_in_flight_item_is_manual_required_without_post_across_replay():
    request = _validate()
    operation = _in_flight_operation(request)
    provider = FakeProvider(_parent())
    store = MemoryStore(operation)
    service = SubtaskService(store)

    first = _run(service, request, provider, operation=operation)
    resumed_operation = service.preflight(request).operation
    second = _run(service, request, provider, operation=resumed_operation)

    assert provider.create_calls == []
    assert len(provider.reconcile_calls) == 2
    assert first.status == second.status == "partial"
    assert first.retryable is second.retryable is True
    assert first.pending[0].manual_required is True
    assert second.pending[0].manual_required is True
    persisted_item = store.operations["attempt-1"].state["items"][0]
    assert store.operations["attempt-1"].status == "partial"
    assert persisted_item["phase"] == "in_flight"
    assert persisted_item["manual_required"] is True


def test_duplicate_reconciliation_marker_requires_manual_choice_and_never_posts():
    request = _validate()
    operation = _in_flight_operation(request)
    marker = operation.state["items"][0]["marker"]
    first = NativeSubtaskIdentity("child-1", "PRI-225", "Первая")
    second = NativeSubtaskIdentity("child-2", "PRI-226", "Вторая")
    provider = FakeProvider(
        _parent(),
        reconciled=[
            ReconciledNativeSubtask(marker, first),
            ReconciledNativeSubtask(marker, second),
        ],
    )
    store = MemoryStore(operation)

    result = _run(SubtaskService(store), request, provider, operation=operation)

    assert provider.create_calls == []
    assert result.pending[0].manual_required is True
    assert result.pending[0].board_id is None
    assert result.warnings == (
        "multiple board cards contain the same idempotency marker",
    )
    item = store.operations["attempt-1"].state["items"][0]
    assert item["board_id"] is None
    assert item["manual_required"] is True


def test_committed_then_timeout_reconciles_on_retry_with_only_one_total_post():
    request = _validate()
    identity = NativeSubtaskIdentity("child-uuid", "PRI-225", "Дочерняя задача")
    provider = FakeProvider(_parent())

    def commit_then_timeout(marker):
        provider.reconciled.append(ReconciledNativeSubtask(marker, identity))
        raise RuntimeError("transport timeout token=SECRET")

    provider.create_effects.append(commit_then_timeout)
    store = MemoryStore()
    service = SubtaskService(store)

    first = _run(
        service,
        request,
        provider,
        sanitize=lambda _value: "redacted failure",
    )
    persisted_after_timeout = deepcopy(store.operations["attempt-1"])
    resumed = service.preflight(request).operation
    second = _run(
        service,
        request,
        provider,
        operation=resumed,
        sanitize=lambda _value: "redacted failure",
    )

    assert len(provider.create_calls) == 1
    assert first.pending[0].manual_required is True
    assert persisted_after_timeout.state["items"][0]["phase"] == "in_flight"
    assert persisted_after_timeout.state["items"][0]["warnings"] == ["redacted failure"]
    assert "SECRET" not in json.dumps(persisted_after_timeout.state)
    assert "SECRET" not in json.dumps(first.payload())
    assert second.status == "partial"
    assert second.created[0].board_id == "child-uuid"
    assert second.unattached == second.created


def test_render_failure_happens_before_in_flight_checkpoint_and_post(monkeypatch):
    request = _validate()
    store = MemoryStore()
    provider = FakeProvider(
        _parent(),
        create_effects=[NativeSubtaskIdentity("child-1", "PRI-225", "Child")],
    )

    def fail_render(_doc):
        raise ValueError("invalid canonical doc")

    monkeypatch.setattr(subtasks_module, "render_markdown", fail_render)

    with pytest.raises(ValueError, match="invalid canonical doc"):
        _run(SubtaskService(store), request, provider)

    persisted = store.operations["attempt-1"]
    assert persisted.revision == 0
    assert persisted.state["items"][0]["phase"] == "pending"
    assert provider.create_calls == []


def test_identity_warning_sanitizer_failure_stays_in_flight_and_never_reposts():
    request = _validate()
    identity = NativeSubtaskIdentity(
        "child-uuid",
        "PRI-225",
        "Child",
        warnings=("token=VERY_SECRET",),
    )
    provider = FakeProvider(_parent(), create_effects=[identity])
    store = MemoryStore()
    service = SubtaskService(store)
    sanitize_calls = []

    def fail_sanitize(value):
        sanitize_calls.append(value)
        raise RuntimeError("sanitizer failed")

    with pytest.raises(RuntimeError, match="sanitizer failed"):
        _run(service, request, provider, sanitize=fail_sanitize)

    persisted = deepcopy(store.operations["attempt-1"])
    assert len(sanitize_calls) == 1
    assert persisted.revision == 1
    assert persisted.state["items"][0]["phase"] == "in_flight"
    assert persisted.state["items"][0]["warnings"] == []
    assert "VERY_SECRET" not in json.dumps(persisted.state)

    marker = persisted.state["items"][0]["marker"]
    provider.reconciled = [
        ReconciledNativeSubtask(
            marker,
            NativeSubtaskIdentity("child-uuid", "PRI-225", "Child"),
        )
    ]
    resumed = service.preflight(request).operation
    result = _run(service, request, provider, operation=resumed)

    assert len(provider.create_calls) == 1
    assert result.created[0].board_id == "child-uuid"


def test_provider_error_sanitizer_failure_is_called_once_and_persists_no_secret():
    request = _validate()
    provider = FakeProvider(
        _parent(),
        create_effects=[RuntimeError("password=VERY_SECRET")],
    )
    store = MemoryStore()
    service = SubtaskService(store)
    sanitize_calls = []

    def fail_sanitize(value):
        sanitize_calls.append(value)
        raise RuntimeError("sanitizer unavailable")

    with pytest.raises(RuntimeError, match="sanitizer unavailable"):
        _run(service, request, provider, sanitize=fail_sanitize)

    persisted = store.operations["attempt-1"]
    assert len(sanitize_calls) == 1
    assert persisted.revision == 1
    assert persisted.state["items"][0]["phase"] == "in_flight"
    assert persisted.state["items"][0]["warnings"] == []
    assert "VERY_SECRET" not in json.dumps(persisted.state)

    resumed = service.preflight(request).operation
    result = _run(service, request, provider, operation=resumed)
    assert result.pending[0].manual_required is True
    assert len(provider.create_calls) == 1


def test_invalid_returned_identity_propagates_without_persisting_provider_data():
    request = _validate()
    provider = FakeProvider(_parent(), create_effects=[object()])
    store = MemoryStore()
    service = SubtaskService(store)

    with pytest.raises(LedgerUnavailableError, match="identity"):
        _run(service, request, provider)

    persisted = store.operations["attempt-1"]
    assert persisted.revision == 1
    assert persisted.state["items"][0]["phase"] == "in_flight"
    assert persisted.state["items"][0]["warnings"] == []
    assert len(provider.create_calls) == 1

    resumed = service.preflight(request).operation
    result = _run(service, request, provider, operation=resumed)
    assert result.pending[0].manual_required is True
    assert len(provider.create_calls) == 1


def test_multi_child_failure_is_sanitized_and_later_child_still_completes():
    second_child = {**CHILD, "title": "Вторая задача"}
    request = _validate(subtasks=[CHILD, second_child])
    provider = FakeProvider(
        _parent(),
        create_effects=[
            RuntimeError("password=VERY_SECRET"),
            NativeSubtaskIdentity("child-2", "PRI-226", "Вторая задача"),
        ],
    )
    store = MemoryStore()

    result = _run(
        SubtaskService(store),
        request,
        provider,
        sanitize=lambda _value: "safe create failure",
    )

    assert len(provider.create_calls) == 2
    assert result.status == "partial"
    assert tuple(child.index for child in result.created) == (1,)
    assert tuple(child.index for child in result.pending) == (0,)
    assert result.pending[0].manual_required is True
    assert result.warnings == ("safe create failure",)
    persisted = store.operations["attempt-1"]
    assert persisted.status == "partial"
    assert persisted.state["items"][0]["phase"] == "in_flight"
    assert persisted.state["items"][1]["phase"] == "created"
    assert "VERY_SECRET" not in json.dumps(persisted.state)


def test_multi_child_checkpoints_capture_independent_immutable_transitions():
    second_child = {**CHILD, "title": "Вторая задача"}
    request = _validate(subtasks=[CHILD, second_child])
    store = MemoryStore()
    provider = FakeProvider(
        _parent(),
        create_effects=[
            NativeSubtaskIdentity("child-1", "PRI-225", "Первая задача"),
            NativeSubtaskIdentity("child-2", "PRI-226", "Вторая задача"),
        ],
    )

    result = _run(SubtaskService(store), request, provider)

    first_hash, second_hash = (_input_hash(draft) for draft in request.subtasks)
    assert store.checkpoint_history == [
        (
            0,
            "partial",
            ((0, "in_flight", first_hash), (1, "pending", second_hash)),
        ),
        (
            1,
            "partial",
            ((0, "created", first_hash), (1, "pending", second_hash)),
        ),
        (
            2,
            "partial",
            ((0, "created", first_hash), (1, "in_flight", second_hash)),
        ),
        (
            3,
            "partial",
            ((0, "created", first_hash), (1, "created", second_hash)),
        ),
        (
            4,
            "partial",
            ((0, "created", first_hash), (1, "created", second_hash)),
        ),
    ]
    assert store.operations["attempt-1"].revision == 5
    assert store.operations["attempt-1"].state["target_subtask_ids"] == [
        "child-1",
        "child-2",
    ]
    assert result.status == "partial"
    assert tuple(child.index for child in result.unattached) == (0, 1)

    store.checkpoint_snapshots[-1].state["items"][0]["phase"] = "mutated"
    store.checkpoint_snapshots[-1].state["items"][0]["input_hash"] = "mutated"
    assert store.checkpoint_snapshots[0].state["items"][0]["phase"] == "in_flight"
    assert store.checkpoint_snapshots[0].state["items"][0]["input_hash"] == first_hash
    assert store.checkpoint_history[-1][2][0] == (0, "created", first_hash)
    assert store.operations["attempt-1"].state["items"][0]["phase"] == "created"


def test_busy_parent_lock_returns_retryable_error_without_child_write():
    request = _validate()
    store = MemoryStore(busy=True)
    provider = FakeProvider(_parent())

    result = _run(SubtaskService(store), request, provider)

    assert result.status == "error"
    assert result.category == "in_progress"
    assert result.retryable is True
    assert provider.create_calls == []
    assert store.operations == {}


def test_dead_parent_lock_after_in_flight_checkpoint_propagates_fail_closed():
    request = _validate()
    store = MemoryStore(dead=True)
    provider = FakeProvider(
        _parent(),
        create_effects=[NativeSubtaskIdentity("child-1", "PRI-225", "Child")],
    )

    with pytest.raises(LedgerUnavailableError, match="dead lock"):
        _run(SubtaskService(store), request, provider)

    assert provider.create_calls == []
    persisted = store.operations["attempt-1"]
    assert persisted.revision == 1
    assert persisted.state["items"][0]["phase"] == "in_flight"


def test_stale_checkpoint_propagates_without_lock_health_check_or_post():
    request = _validate()
    events = []
    store = MemoryStore(events=events, stale=True)
    provider = FakeProvider(
        _parent(),
        events=events,
        create_effects=[NativeSubtaskIdentity("child-1", "PRI-225", "Child")],
    )

    with pytest.raises(OperationConflictError, match="stale revision"):
        _run(SubtaskService(store), request, provider)

    assert not any(event[0] == "ensure_alive" for event in events)
    assert provider.create_calls == []


@pytest.mark.parametrize(
    "case",
    [
        "state_not_object",
        "request_payload_not_object",
        "revision_negative",
        "revision_bool",
        "items_not_list",
        "item_count",
        "duplicate_indices",
        "wrong_title",
        "wrong_input_hash",
        "wrong_marker",
        "invalid_phase",
        "invalid_manual",
        "invalid_warnings",
        "pending_identity",
        "in_flight_key_only",
        "in_flight_board_only",
        "in_flight_aliases_only",
        "in_flight_url_only",
        "in_flight_complete_identity",
        "created_without_board_id",
        "created_manual_required",
        "attached_without_key",
        "complete_attached_manual_required",
        "invalid_aliases",
        "blank_alias",
        "invalid_url",
        "invalid_status",
        "running_in_flight",
        "complete_pending",
        "board_complete_created",
        "request_subtasks_not_list",
        "request_hash_mismatch",
        "board_type_mismatch",
    ],
)
def test_malformed_persisted_operation_fails_closed_before_provider_or_callback(case):
    request = _validate(subtasks=[CHILD, {**CHILD, "title": "Вторая задача"}])
    operation = _malformed_operation(request, case)
    store = MemoryStore(operation)
    provider = FakeProvider(
        _parent(),
        create_effects=[
            NativeSubtaskIdentity("child-1", "PRI-225", "Первая"),
            NativeSubtaskIdentity("child-2", "PRI-226", "Вторая"),
        ],
    )
    write_calls = []

    with pytest.raises(LedgerUnavailableError):
        _run(
            SubtaskService(store),
            request,
            provider,
            operation=operation,
            write_calls=write_calls,
        )

    assert provider.fetch_calls == []
    assert provider.reconcile_calls == []
    assert provider.create_calls == []
    assert write_calls == []


def test_validator_ignores_safe_unknown_additive_ledger_fields():
    request = _validate()
    operation = _pending_operation(request)
    operation.state["future_state"] = {"version": 2}
    operation.state["items"][0]["future_item"] = ["safe"]
    operation.request_payload["future_payload"] = {"enabled": True}
    operation = replace(
        operation,
        request_hash=_request_payload_hash(operation.request_payload),
    )

    preflight = SubtaskService(MemoryStore(operation)).preflight(request)

    assert preflight.operation == operation
    assert preflight.result is not None
    assert preflight.result.category == "conflict"


def test_incomplete_ledger_with_missing_item_fails_closed_without_post():
    request = _validate()
    operation = _operation(request, state={"revision": 2, "items": []})
    store = MemoryStore(operation)
    provider = FakeProvider(_parent())

    with pytest.raises(LedgerUnavailableError, match="число подзадач"):
        _run(SubtaskService(store), request, provider, operation=operation)

    assert provider.reconcile_calls == []
    assert provider.create_calls == []


def test_pending_item_with_wrong_marker_fails_closed_without_post():
    request = _validate()
    operation = _in_flight_operation(request)
    operation.state["items"][0]["phase"] = "pending"
    operation.state["items"][0]["marker"] = "reviewer-subtask:" + "f" * 64
    store = MemoryStore(operation)
    provider = FakeProvider(_parent())

    with pytest.raises(LedgerUnavailableError, match="marker"):
        _run(SubtaskService(store), request, provider, operation=operation)

    assert provider.create_calls == []


def test_resume_rejects_item_input_hash_that_does_not_match_canonical_draft():
    request = _validate()
    operation = _in_flight_operation(request)
    operation.state["items"][0]["input_hash"] = "f" * 64
    store = MemoryStore(operation)
    provider = FakeProvider(_parent())

    with pytest.raises(LedgerUnavailableError, match="input_hash"):
        _run(SubtaskService(store), request, provider, operation=operation)

    assert provider.reconcile_calls == []
    assert provider.create_calls == []


def test_complete_preflight_rejects_malformed_item_input_hash():
    request = _validate()
    operation = _in_flight_operation(request)
    operation.state["items"][0]["phase"] = "attached"
    operation.state["items"][0]["input_hash"] = "f" * 64
    complete = replace(operation, status="complete")

    with pytest.raises(LedgerUnavailableError, match="input_hash"):
        SubtaskService(MemoryStore(complete)).preflight(request)


def test_missing_parent_is_deterministic_nonretryable_error():
    request = _validate()
    store = MemoryStore()
    provider = FakeProvider(None)

    first = _run(SubtaskService(store), request, provider)
    second = _run(SubtaskService(store), request, provider)

    assert first == second
    assert first.status == "error"
    assert first.category == "parent_not_found"
    assert first.retryable is False
    assert store.operations == {}
    assert provider.create_calls == []


def test_run_reloads_incomplete_row_before_parent_fetch_after_stale_preflight():
    request = _validate()
    store = MemoryStore()
    service = SubtaskService(store)
    assert service.preflight(request) == SubtaskPreflight(None, None)

    operation = _in_flight_operation(request)
    marker = operation.state["items"][0]["marker"]
    store.operations[request.idempotency_key] = deepcopy(operation)
    provider = FakeProvider(
        _parent(),
        reconciled=[
            ReconciledNativeSubtask(
                marker,
                NativeSubtaskIdentity("child-uuid", "PRI-225", "Child"),
            )
        ],
    )

    result = _run(service, request, provider)

    assert provider.fetch_calls == ["parent-uuid", "parent-uuid"]
    assert provider.create_calls == []
    assert result.resumed is True
    assert result.created[0].board_id == "child-uuid"


def test_run_replays_row_completed_after_stale_preflight_without_parent_fetch():
    request = _validate()
    store = MemoryStore()
    service = SubtaskService(store)
    assert service.preflight(request) == SubtaskPreflight(None, None)

    store.operations[request.idempotency_key] = _attached_operation(request)
    provider = FakeProvider(None)

    result = _run(service, request, provider)

    assert result.status == "ok"
    assert result.resumed is True
    assert provider.fetch_calls == []
    assert provider.create_calls == []


def test_failed_parent_fetch_reloads_concurrent_progress_instead_of_not_found():
    request = _validate()
    operation = _in_flight_operation(request)
    marker = operation.state["items"][0]["marker"]
    store = MemoryStore()

    class RacingProvider(FakeProvider):
        def fetch_one(self, key):
            self.fetch_calls.append(key)
            if len(self.fetch_calls) == 1:
                store.operations[request.idempotency_key] = deepcopy(operation)
                return None
            return _parent()

    provider = RacingProvider(
        None,
        reconciled=[
            ReconciledNativeSubtask(
                marker,
                NativeSubtaskIdentity("child-uuid", "PRI-225", "Child"),
            )
        ],
    )

    result = _run(SubtaskService(store), request, provider)

    assert result.category != "parent_not_found"
    assert result.resumed is True
    assert result.created[0].board_id == "child-uuid"
    assert provider.fetch_calls == ["PRI-224", "parent-uuid", "parent-uuid"]
    assert provider.create_calls == []


def test_invalid_source_metadata_reloads_concurrent_complete_row_before_error():
    request = _validate()
    complete = _attached_operation(request)
    store = MemoryStore()

    class RacingProvider(FakeProvider):
        def fetch_one(self, key):
            self.fetch_calls.append(key)
            store.operations[request.idempotency_key] = deepcopy(complete)
            return _parent(provider_data=None)

    provider = RacingProvider(None)

    result = _run(SubtaskService(store), request, provider)

    assert result.status == "ok"
    assert result.category is None
    assert provider.fetch_calls == ["PRI-224"]
    assert provider.create_calls == []


@pytest.mark.parametrize(
    "parent",
    [
        _parent(board_id=""),
        _parent(board_id="  "),
        _parent(provider_data=None),
        _parent(provider_data={"source_column_id": "column-uuid"}),
        _parent(provider_data={"source_board_id": "board-uuid"}),
        _parent(
            provider_data={
                "source_board_id": "board-uuid",
                "source_column_id": "  ",
            }
        ),
    ],
)
def test_missing_required_source_identity_fails_before_ledger_or_child_write(parent):
    request = _validate()
    store = MemoryStore()
    provider = FakeProvider(parent)

    result = _run(SubtaskService(store), request, provider)

    assert result.status == "error"
    assert result.category == "source_metadata_missing"
    assert result.retryable is False
    assert store.operations == {}
    assert provider.create_calls == []


def test_run_reloads_concurrent_conflict_without_provider_write_or_callback():
    request = _validate()
    conflicting = _pending_operation(_validate(parent_key="PRI-999"))
    store = MemoryStore(conflicting)
    provider = FakeProvider(_parent())
    write_calls = []

    result = _run(
        SubtaskService(store),
        request,
        provider,
        write_calls=write_calls,
    )

    assert result.category == "conflict"
    assert result.retryable is False
    assert provider.fetch_calls == []
    assert provider.reconcile_calls == []
    assert provider.create_calls == []
    assert provider.replace_calls == []
    assert write_calls == []


def test_run_replays_complete_operation_without_provider_or_callback():
    request = _validate()
    complete = _attached_operation(request)
    store = MemoryStore(complete)
    provider = FakeProvider(_parent())
    write_calls = []

    result = _run(
        SubtaskService(store),
        request,
        provider,
        operation=complete,
        write_calls=write_calls,
    )

    assert result.status == "ok"
    assert result.resumed is True
    assert result.created == result.attached
    assert result.unattached == ()
    assert provider.fetch_calls == []
    assert provider.reconcile_calls == []
    assert provider.create_calls == []
    assert provider.replace_calls == []
    assert write_calls == []


def test_concurrent_same_key_insert_is_reloaded_and_terminal_replay_is_safe():
    request = _validate()
    complete = _attached_operation(request)
    store = MemoryStore(insert_race=complete)
    provider = FakeProvider(_parent())

    result = _run(SubtaskService(store), request, provider)

    assert result.status == "ok"
    assert result.resumed is True
    assert provider.reconcile_calls == []
    assert provider.create_calls == []


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("parent_key", "PRI\0-224"),
        ("idempotency_key", "attempt\0-1"),
        ("board_type", "you\0gile"),
        ("project", "P\0RI"),
    ],
)
def test_nul_in_request_identity_is_rejected(field, value):
    with pytest.raises(ValueError):
        _validate(**{field: value})


@pytest.mark.parametrize("value", [None, "", " \t"])
def test_optional_board_text_is_normalized_to_none(value):
    request = _validate(board_type=value, project=value)

    assert request.board_type is None
    assert request.project is None


def test_optional_board_text_is_trimmed():
    request = _validate(board_type="  yougile  ", project="  PRI  ")

    assert request.board_type == "yougile"
    assert request.project == "PRI"


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


def test_provider_options_default_and_none_are_normalized_to_empty_dict():
    default_options = validate_subtask_request(
        parent_key="PRI-224",
        subtasks=[CHILD],
        idempotency_key="attempt-1",
        board_type=None,
        project=None,
    )
    explicit_none = _validate(provider_options=None)

    assert default_options.provider_options == {}
    assert explicit_none.provider_options == {}


def test_nested_provider_options_are_snapshotted_before_hashing():
    options = {"mapping": {"columns": ["todo"]}}

    request = _validate(provider_options=options)
    original_hash = request.request_hash
    options["mapping"]["columns"].append("done")

    assert request.provider_options == {"mapping": {"columns": ["todo"]}}
    assert request.request_hash == original_hash


def test_non_finite_option_is_rejected_by_canonical_json():
    with pytest.raises(ValueError):
        _validate(provider_options={"weight": float("nan")})


def test_non_json_option_is_rejected_by_canonical_json():
    with pytest.raises(TypeError):
        _validate(provider_options={"value": object()})


def test_validated_request_can_generate_marker():
    request = _validate(
        parent_key="  PRI-224  ",
        idempotency_key="  attempt-1  ",
        board_type="  yougile  ",
    )

    marker = marker_for(
        request.board_type,
        request.parent_key,
        request.idempotency_key,
        0,
        request.subtasks[0],
    )

    assert SUBTASK_MARKER_RE.fullmatch(marker)


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


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("reviewer-subtask:" + "a" * 64, True),
        ("reviewer-subtask:" + "a" * 63, False),
        ("reviewer-subtask:" + "a" * 65, False),
        ("reviewer-subtask:" + "A" * 64, False),
    ],
)
def test_public_marker_regex_matches_and_strips_only_exact_tokens(token, expected):
    text = f"before {token} after"

    assert bool(SUBTASK_MARKER_RE.search(text)) is expected
    assert (SUBTASK_MARKER_RE.sub("", text) != text) is expected
