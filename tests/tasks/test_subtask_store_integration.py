from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest

from reviewer.config.settings import Settings
from reviewer.tasks.subtask_store import (
    OperationConflictError,
    SubtaskOperation,
    SubtaskOperationStore,
)

pytestmark = pytest.mark.integration


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _operation(
    idempotency_key: str,
    parent_task_id: str,
    *,
    status: str = "complete",
) -> SubtaskOperation:
    return SubtaskOperation(
        idempotency_key=idempotency_key,
        board_type="yougile",
        parent_input_key=_key("parent-input"),
        parent_task_id=parent_task_id,
        source_board_id=_key("board"),
        source_column_id=_key("column"),
        request_hash=_key("request-hash"),
        request_payload={
            "title": "Интеграционная подзадача",
            "criteria": ["создана ровно один раз"],
        },
        state={
            "revision": 0,
            "created": [{"key": _key("child"), "status": "created"}],
        },
        status=status,
    )


def _delete_operations(dsn: str, keys: list[str]) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "DELETE FROM subtask_operations WHERE idempotency_key = ANY(%s)",
            (keys,),
        )


def test_complete_operation_survives_store_restart_with_exact_snapshot() -> None:
    dsn = Settings().pg_dsn
    operation = _operation(_key("operation"), _key("parent"))
    first = SubtaskOperationStore(dsn)
    second: SubtaskOperationStore | None = None

    try:
        persisted = first.insert(operation)
        first.close()

        second = SubtaskOperationStore(dsn)
        loaded = second.load(operation.idempotency_key)

        assert loaded == persisted
        assert loaded is not None
        assert (
            loaded.idempotency_key,
            loaded.board_type,
            loaded.parent_input_key,
            loaded.parent_task_id,
            loaded.source_board_id,
            loaded.source_column_id,
            loaded.request_hash,
        ) == (
            operation.idempotency_key,
            operation.board_type,
            operation.parent_input_key,
            operation.parent_task_id,
            operation.source_board_id,
            operation.source_column_id,
            operation.request_hash,
        )
        assert loaded.request_payload == operation.request_payload
        assert loaded.state == operation.state
        assert loaded.status == "complete"
        assert loaded.revision == 0
    finally:
        first.close()
        if second is not None:
            second.close()
        _delete_operations(dsn, [operation.idempotency_key])


def test_parent_lock_contends_by_parent_and_reacquires_after_release() -> None:
    dsn = Settings().pg_dsn
    parent_task_id = _key("parent")
    other_parent_task_id = _key("parent")
    first_operation = _operation(_key("operation"), parent_task_id, status="running")
    second_operation = _operation(_key("operation"), parent_task_id, status="running")
    first = SubtaskOperationStore(dsn)
    second = SubtaskOperationStore(dsn)

    try:
        first.insert(first_operation)
        second.insert(second_operation)
        assert first_operation.idempotency_key != second_operation.idempotency_key

        with first.try_parent_lock("yougile", parent_task_id) as held:
            assert held is not None
            with second.try_parent_lock("yougile", parent_task_id) as contended:
                assert contended is None
            with second.try_parent_lock("yougile", other_parent_task_id) as independent:
                assert independent is not None

        with second.try_parent_lock("yougile", parent_task_id) as reacquired:
            assert reacquired is not None
    finally:
        first.close()
        second.close()
        _delete_operations(
            dsn,
            [first_operation.idempotency_key, second_operation.idempotency_key],
        )


def test_checkpoint_cas_rejects_stale_revision_across_store_instances() -> None:
    dsn = Settings().pg_dsn
    operation = _operation(_key("operation"), _key("parent"), status="running")
    first = SubtaskOperationStore(dsn)
    second = SubtaskOperationStore(dsn)

    try:
        revision_zero = first.insert(operation)
        stale_revision_zero = second.load(operation.idempotency_key)
        assert stale_revision_zero is not None

        winner = first.checkpoint(
            replace(
                revision_zero,
                state={"revision": 0, "created": ["winner"]},
                status="complete",
            ),
            expected_revision=0,
        )

        with pytest.raises(OperationConflictError):
            second.checkpoint(
                replace(
                    stale_revision_zero,
                    state={"revision": 0, "created": ["stale-overwrite"]},
                    status="partial",
                ),
                expected_revision=0,
            )

        reloaded = second.load(operation.idempotency_key)
        assert reloaded is not None
        assert reloaded.state == winner.state == {"revision": 1, "created": ["winner"]}
        assert reloaded.status == winner.status == "complete"
        assert reloaded.revision == winner.revision == 1
    finally:
        first.close()
        second.close()
        _delete_operations(dsn, [operation.idempotency_key])
