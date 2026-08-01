from __future__ import annotations

import copy
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Lock

import pytest
from psycopg.types.json import Jsonb

from reviewer.tasks.subtask_store import (
    LedgerUnavailableError,
    OperationConflictError,
    SubtaskOperation,
    SubtaskOperationStore,
)

_CREATED_AT = datetime(2026, 8, 1, 10, tzinfo=UTC)
_UPDATED_AT = datetime(2026, 8, 1, 11, tzinfo=UTC)


def _operation(**changes) -> SubtaskOperation:
    operation = SubtaskOperation(
        idempotency_key="idem-1",
        board_type="yougile",
        parent_input_key="PRI-224",
        parent_task_id="parent-1",
        source_board_id="board-1",
        source_column_id="column-1",
        request_hash="hash-1",
        request_payload={"title": "Дочерняя задача"},
        state={"created": [], "revision": 0},
        status="running",
        created_at=None,
        updated_at=None,
    )
    return replace(operation, **changes)


class _Result:
    def __init__(self, row: tuple | None) -> None:
        self._row = row

    def fetchone(self) -> tuple | None:
        return self._row


class _Database:
    def __init__(self) -> None:
        self.rows: dict[str, tuple] = {}
        self.schema_calls = 0
        self.schema_error: Exception | None = None
        self.load_error: Exception | None = None
        self.acquire_error: Exception | None = None
        self.acquire_commit_error: Exception | None = None
        self.unlock_error: Exception | None = None
        self.unlock_commit_error: Exception | None = None
        self.lock_results: dict[str, bool] = {}
        self.lock_calls: list[tuple[_Connection, tuple]] = []
        self.unlock_calls: list[tuple[_Connection, tuple]] = []
        self.last_insert_params: tuple | None = None
        self.last_checkpoint_sql = ""
        self._lock = Lock()

    def execute(self, connection: _Connection, sql: str, params: tuple | None) -> _Result:
        compact_sql = " ".join(sql.split())
        if compact_sql.startswith("CREATE TABLE IF NOT EXISTS subtask_operations"):
            with self._lock:
                self.schema_calls += 1
            if self.schema_error is not None:
                raise self.schema_error
            return _Result(None)
        if compact_sql.startswith("SELECT idempotency_key"):
            if self.load_error is not None:
                raise self.load_error
            assert params is not None
            return _Result(self.rows.get(params[0]))
        if compact_sql.startswith("INSERT INTO subtask_operations"):
            assert params is not None
            self.last_insert_params = params
            row = (*params[:7], params[7].obj, params[8].obj, params[9], _CREATED_AT, _UPDATED_AT)
            self.rows[params[0]] = row
            return _Result(row)
        if compact_sql.startswith("UPDATE subtask_operations"):
            assert params is not None
            self.last_checkpoint_sql = compact_sql
            state, status, idempotency_key, expected_revision = params
            old = self.rows.get(idempotency_key)
            if old is None or int(old[8].get("revision", 0)) != expected_revision:
                return _Result(None)
            row = (*old[:8], state.obj, status, old[10], _UPDATED_AT)
            self.rows[idempotency_key] = row
            return _Result(row)
        if "pg_try_advisory_lock" in compact_sql:
            if self.acquire_error is not None:
                raise self.acquire_error
            assert params is not None
            self.lock_calls.append((connection, params))
            acquired = self.lock_results.get(params[1], True)
            if acquired:
                connection.next_commit_error = self.acquire_commit_error
            return _Result((acquired,))
        if "pg_advisory_unlock" in compact_sql:
            assert params is not None
            self.unlock_calls.append((connection, params))
            if self.unlock_error is not None:
                raise self.unlock_error
            connection.next_commit_error = self.unlock_commit_error
            return _Result((True,))
        if compact_sql == "SELECT 1":
            if connection.health_error is not None:
                raise connection.health_error
            return _Result((1,) if connection.alive else None)
        raise AssertionError(f"Unexpected SQL: {compact_sql}")


class _Connection:
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.checked_out = False
        self.commits = 0
        self.alive = True
        self.closed = False
        self.close_calls = 0
        self.closed_while_checked_out = False
        self.health_error: Exception | None = None
        self.next_commit_error: Exception | None = None

    def execute(self, sql: str, params: tuple | None = None) -> _Result:
        return self.database.execute(self, sql, params)

    def commit(self) -> None:
        self.commits += 1
        if self.next_commit_error is not None:
            error = self.next_commit_error
            self.next_commit_error = None
            raise error

    def close(self) -> None:
        self.close_calls += 1
        self.closed_while_checked_out = self.checked_out
        self.closed = True
        self.alive = False


class _ConnectionContext:
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool
        self.connection = _Connection(pool.database)

    def __enter__(self) -> _Connection:
        if self.pool.connection_error is not None:
            raise self.pool.connection_error
        self.connection.checked_out = True
        self.pool.connections.append(self.connection)
        return self.connection

    def __exit__(self, *_args) -> bool:
        self.connection.checked_out = False
        return False


class _Pool:
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.connections: list[_Connection] = []
        self.open_calls = 0
        self.close_calls = 0
        self.connection_error: Exception | None = None

    def open(self) -> None:
        self.open_calls += 1

    def connection(self) -> _ConnectionContext:
        return _ConnectionContext(self)

    def close(self) -> None:
        self.close_calls += 1


class _PoolFactory:
    def __init__(self, database: _Database | None = None) -> None:
        self.database = database or _Database()
        self.calls: list[tuple[str, dict]] = []
        self.pools: list[_Pool] = []

    def __call__(self, dsn: str, **kwargs) -> _Pool:
        self.calls.append((dsn, kwargs))
        pool = _Pool(self.database)
        self.pools.append(pool)
        return pool


def test_operation_is_frozen_and_exposes_revision() -> None:
    operation = _operation(state={})

    assert operation.revision == 0
    assert [field.name for field in fields(operation)] == [
        "idempotency_key",
        "board_type",
        "parent_input_key",
        "parent_task_id",
        "source_board_id",
        "source_column_id",
        "request_hash",
        "request_payload",
        "state",
        "status",
        "created_at",
        "updated_at",
    ]
    with pytest.raises(FrozenInstanceError):
        operation.status = "complete"  # type: ignore[misc]


def test_operation_database_timestamps_default_to_none() -> None:
    operation = SubtaskOperation(
        idempotency_key="idem-1",
        board_type="yougile",
        parent_input_key="PRI-224",
        parent_task_id="parent-1",
        source_board_id="board-1",
        source_column_id="column-1",
        request_hash="hash-1",
        request_payload={},
        state={},
        status="running",
    )

    assert operation.created_at is None
    assert operation.updated_at is None


def test_construction_is_lazy_and_first_load_opens_pool_and_installs_schema() -> None:
    factory = _PoolFactory()
    store = SubtaskOperationStore(
        "postgresql://ledger",
        min_size=2,
        max_size=7,
        pool_factory=factory,
    )

    assert factory.calls == []
    assert store.load("missing") is None

    assert factory.calls == [
        ("postgresql://ledger", {"min_size": 2, "max_size": 7, "open": False})
    ]
    assert factory.pools[0].open_calls == 1
    assert factory.database.schema_calls == 1


def test_schema_installation_is_once_under_concurrent_loads() -> None:
    factory = _PoolFactory()
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)
    barrier = Barrier(8)

    def load() -> None:
        barrier.wait()
        assert store.load("missing") is None

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _index: load(), range(8)))

    assert len(factory.calls) == 1
    assert factory.database.schema_calls == 1


def test_insert_and_load_convert_rows_and_use_jsonb_params() -> None:
    factory = _PoolFactory()
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)
    operation = _operation()

    inserted = store.insert(operation)
    loaded = store.load(operation.idempotency_key)

    assert inserted == loaded == replace(
        operation,
        created_at=_CREATED_AT,
        updated_at=_UPDATED_AT,
    )
    assert inserted is not operation
    params = factory.database.last_insert_params
    assert params is not None
    assert isinstance(params[7], Jsonb)
    assert params[7].obj == {"title": "Дочерняя задача"}
    assert isinstance(params[8], Jsonb)
    assert params[8].obj == {"created": [], "revision": 0}


def test_checkpoint_increments_revision_without_mutating_input_or_identity() -> None:
    factory = _PoolFactory()
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)
    persisted = store.insert(_operation(state={"created": ["child-1"], "revision": 2}))
    candidate = replace(
        persisted,
        board_type="must-not-be-written",
        request_hash="must-not-be-written",
        request_payload={"must": "not be written"},
        state={"created": ["child-1", "child-2"], "revision": 2},
        status="board_complete",
    )
    original = copy.deepcopy(candidate)

    checkpointed = store.checkpoint(candidate, expected_revision=2)

    assert candidate == original
    assert checkpointed.revision == 3
    assert checkpointed.state == {"created": ["child-1", "child-2"], "revision": 3}
    assert checkpointed.status == "board_complete"
    assert checkpointed.board_type == persisted.board_type
    assert checkpointed.request_hash == persisted.request_hash
    assert checkpointed.request_payload == persisted.request_payload
    update_sql = factory.database.last_checkpoint_sql
    assert "SET state = %s::jsonb, status = %s, updated_at = now()" in update_sql
    assert "(state ->> 'revision')::bigint" in update_sql
    assert "::integer" not in update_sql
    assert "request_hash =" not in update_sql
    assert "request_payload =" not in update_sql


def test_checkpoint_raises_conflict_for_stale_revision() -> None:
    factory = _PoolFactory()
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)
    operation = store.insert(_operation())
    store.checkpoint(operation, expected_revision=0)

    with pytest.raises(OperationConflictError):
        store.checkpoint(operation, expected_revision=0)


def test_parent_lock_holds_same_connection_and_unlocks_in_finally() -> None:
    factory = _PoolFactory()
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)

    class BodyError(Exception):
        pass

    with pytest.raises(BodyError), store.try_parent_lock(
        "yougile", "parent-1"
    ) as parent_lock:
        assert parent_lock is not None
        connection = factory.database.lock_calls[-1][0]
        assert parent_lock._connection is connection
        assert connection.checked_out is True
        assert factory.database.unlock_calls == []
        raise BodyError

    assert factory.database.unlock_calls == [(connection, ("yougile", "parent-1"))]
    assert connection.checked_out is False
    assert connection.commits == 2
    assert connection.closed is False


def test_contended_parent_lock_yields_none_without_unlock() -> None:
    factory = _PoolFactory()
    factory.database.lock_results["parent-busy"] = False
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)

    with store.try_parent_lock("yougile", "parent-busy") as parent_lock:
        assert parent_lock is None

    assert factory.database.unlock_calls == []
    connection = factory.database.lock_calls[0][0]
    assert connection.commits == 1
    assert connection.checked_out is False
    assert connection.closed is False


def test_acquired_parent_lock_commit_failure_attempts_unlock_and_propagates() -> None:
    factory = _PoolFactory()
    commit_error = RuntimeError("acquisition commit failed")
    factory.database.acquire_commit_error = commit_error
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)

    with pytest.raises(RuntimeError) as exc_info:
        _enter_parent_lock(store)

    assert exc_info.value is commit_error
    connection = factory.database.lock_calls[0][0]
    assert factory.database.unlock_calls == [(connection, ("yougile", "parent-1"))]
    assert connection.commits == 2
    assert connection.checked_out is False


def test_acquisition_error_remains_primary_when_unlock_also_fails() -> None:
    factory = _PoolFactory()
    commit_error = RuntimeError("acquisition commit failed")
    factory.database.acquire_commit_error = commit_error
    factory.database.unlock_error = RuntimeError("unlock failed")
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)

    with pytest.raises(RuntimeError) as exc_info:
        _enter_parent_lock(store)

    assert exc_info.value is commit_error
    connection = factory.database.lock_calls[0][0]
    assert factory.database.unlock_calls == [(connection, ("yougile", "parent-1"))]
    assert connection.checked_out is False


@pytest.mark.parametrize("failure_phase", ["execute", "commit"])
def test_unlock_failure_discards_connection_and_propagates_cleanup_error(
    failure_phase: str,
) -> None:
    factory = _PoolFactory()
    cleanup_error = RuntimeError(f"unlock {failure_phase} failed")
    if failure_phase == "execute":
        factory.database.unlock_error = cleanup_error
    else:
        factory.database.unlock_commit_error = cleanup_error
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)

    with pytest.raises(RuntimeError) as exc_info:
        _enter_parent_lock(store)

    assert exc_info.value is cleanup_error
    connection = factory.database.lock_calls[0][0]
    assert connection.close_calls == 1
    assert connection.closed is True
    assert connection.closed_while_checked_out is True
    assert connection.checked_out is False


@pytest.mark.parametrize("failure_phase", ["execute", "commit"])
def test_unlock_failure_discards_connection_and_preserves_primary_error(
    failure_phase: str,
) -> None:
    factory = _PoolFactory()
    cleanup_error = RuntimeError(f"unlock {failure_phase} failed")
    if failure_phase == "execute":
        factory.database.unlock_error = cleanup_error
    else:
        factory.database.unlock_commit_error = cleanup_error
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)
    primary_error = RuntimeError("body failed")

    with pytest.raises(RuntimeError) as exc_info, store.try_parent_lock(
        "yougile", "parent-1"
    ):
        raise primary_error

    assert exc_info.value is primary_error
    connection = factory.database.lock_calls[0][0]
    assert connection.close_calls == 1
    assert connection.closed is True
    assert connection.closed_while_checked_out is True
    assert connection.checked_out is False


def test_parent_lock_parameters_include_each_parent_id() -> None:
    factory = _PoolFactory()
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)

    with store.try_parent_lock("yougile", "parent-1"):
        pass
    with store.try_parent_lock("yougile", "parent-2"):
        pass

    assert [params for _connection, params in factory.database.lock_calls] == [
        ("yougile", "parent-1"),
        ("yougile", "parent-2"),
    ]
    assert [params for _connection, params in factory.database.unlock_calls] == [
        ("yougile", "parent-1"),
        ("yougile", "parent-2"),
    ]


def test_parent_lock_ensure_alive_fails_closed_with_original_cause() -> None:
    factory = _PoolFactory()
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)
    disconnect = OSError("connection lost")

    with store.try_parent_lock("yougile", "parent-1") as parent_lock:
        assert parent_lock is not None
        parent_lock._connection.health_error = disconnect
        with pytest.raises(LedgerUnavailableError) as exc_info:
            parent_lock.ensure_alive()

    assert exc_info.value.__cause__ is disconnect


@pytest.mark.parametrize("failure", ["schema", "load", "acquire", "unlock", "connection"])
def test_database_and_lock_errors_propagate(failure: str) -> None:
    factory = _PoolFactory()
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)
    expected = RuntimeError(f"{failure} failed")

    if failure == "schema":
        factory.database.schema_error = expected
        call = lambda: store.load("missing")
    elif failure == "load":
        factory.database.load_error = expected
        call = lambda: store.load("missing")
    elif failure == "acquire":
        factory.database.acquire_error = expected
        call = lambda: _enter_parent_lock(store)
    elif failure == "unlock":
        factory.database.unlock_error = expected
        call = lambda: _enter_parent_lock(store)
    else:
        store.load("missing")
        factory.pools[0].connection_error = expected
        call = lambda: store.load("missing")

    with pytest.raises(RuntimeError) as exc_info:
        call()
    assert exc_info.value is expected


def _enter_parent_lock(store: SubtaskOperationStore) -> None:
    with store.try_parent_lock("yougile", "parent-1"):
        pass


def test_close_resets_pool_and_schema_for_safe_reuse() -> None:
    factory = _PoolFactory()
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)

    assert store.load("missing") is None
    first_pool = factory.pools[0]
    store.close()
    assert first_pool.close_calls == 1

    assert store.load("missing") is None
    assert len(factory.pools) == 2
    assert factory.pools[1].open_calls == 1
    assert factory.database.schema_calls == 2


def test_subtask_schema_matches_contract_and_is_packaged() -> None:
    root = Path(__file__).parents[2]
    schema = (root / "reviewer/tasks/subtask_store.sql").read_text(encoding="utf-8")
    expected = """\
CREATE TABLE IF NOT EXISTS subtask_operations (
    idempotency_key  text PRIMARY KEY,
    board_type       text NOT NULL,
    parent_input_key text NOT NULL,
    parent_task_id   text NOT NULL,
    source_board_id  text NOT NULL,
    source_column_id text NOT NULL,
    request_hash     text NOT NULL,
    request_payload  jsonb NOT NULL,
    state            jsonb NOT NULL,
    status           text NOT NULL CHECK (
        status IN ('running', 'partial', 'board_complete', 'complete')
    ),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
"""
    package_data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"
    ]["setuptools"]["package-data"]

    assert schema == expected
    assert package_data["reviewer.tasks"] == ["*.sql"]
