from __future__ import annotations

import copy
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest
from psycopg.types.json import Jsonb

from reviewer.tasks import subtask_store as subtask_store_module
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
        self.schema_connections: list[_Connection] = []
        self.schema_error: Exception | None = None
        self.load_error: Exception | None = None
        self.acquire_error: Exception | None = None
        self.acquire_commit_error: Exception | None = None
        self.unlock_error: Exception | None = None
        self.unlock_commit_error: Exception | None = None
        self.health_commit_error: Exception | None = None
        self.health_row: tuple | None = (1,)
        self.lock_results: dict[str, bool] = {}
        self.lock_rows: dict[str, tuple | None] = {}
        self.lock_calls: list[tuple[_Connection, tuple]] = []
        self.unlock_calls: list[tuple[_Connection, tuple]] = []
        self.health_calls: list[_Connection] = []
        self.last_insert_params: tuple | None = None
        self.last_checkpoint_params: tuple | None = None
        self.last_result_row: tuple | None = None
        self.last_checkpoint_sql = ""
        self._lock = Lock()
        self.held_locks: dict[tuple[str, str], _Connection] = {}

    @staticmethod
    def _jsonb_roundtrip(row: tuple) -> tuple:
        return (*row[:7], copy.deepcopy(row[7]), copy.deepcopy(row[8]), *row[9:])

    def _result(self, row: tuple | None) -> _Result:
        if row is None:
            return _Result(None)
        self.last_result_row = self._jsonb_roundtrip(row)
        return _Result(self.last_result_row)

    def execute(self, connection: _Connection, sql: str, params: tuple | None) -> _Result:
        compact_sql = " ".join(sql.split())
        if compact_sql.startswith("CREATE TABLE IF NOT EXISTS subtask_operations"):
            with self._lock:
                self.schema_calls += 1
                self.schema_connections.append(connection)
            if self.schema_error is not None:
                raise self.schema_error
            return _Result(None)
        if compact_sql.startswith("SELECT idempotency_key"):
            if self.load_error is not None:
                raise self.load_error
            assert params is not None
            return self._result(self.rows.get(params[0]))
        if compact_sql.startswith("INSERT INTO subtask_operations"):
            assert params is not None
            self.last_insert_params = params
            row = (
                *params[:7],
                copy.deepcopy(params[7].obj),
                copy.deepcopy(params[8].obj),
                params[9],
                _CREATED_AT,
                _UPDATED_AT,
            )
            self.rows[params[0]] = row
            return self._result(row)
        if compact_sql.startswith("UPDATE subtask_operations"):
            assert params is not None
            self.last_checkpoint_sql = compact_sql
            self.last_checkpoint_params = params
            state, status, idempotency_key, expected_revision = params
            old = self.rows.get(idempotency_key)
            if old is None or int(old[8].get("revision", 0)) != expected_revision:
                return _Result(None)
            row = (*old[:8], copy.deepcopy(state.obj), status, old[10], _UPDATED_AT)
            self.rows[idempotency_key] = row
            return self._result(row)
        if "pg_try_advisory_lock" in compact_sql:
            if self.acquire_error is not None:
                raise self.acquire_error
            assert params is not None
            self.lock_calls.append((connection, params))
            if params[1] in self.lock_rows:
                return _Result(self.lock_rows[params[1]])
            if params[1] in self.lock_results:
                acquired = self.lock_results[params[1]]
            else:
                with self._lock:
                    lock_key = (params[0], params[1])
                    acquired = lock_key not in self.held_locks
                    if acquired:
                        self.held_locks[lock_key] = connection
            if acquired:
                connection.next_commit_error = self.acquire_commit_error
            return _Result((acquired,))
        if "pg_advisory_unlock" in compact_sql:
            assert params is not None
            self.unlock_calls.append((connection, params))
            if self.unlock_error is not None:
                raise self.unlock_error
            with self._lock:
                lock_key = (params[0], params[1])
                if self.held_locks.get(lock_key) is connection:
                    del self.held_locks[lock_key]
            connection.next_commit_error = self.unlock_commit_error
            return _Result((True,))
        if compact_sql == "SELECT 1":
            self.health_calls.append(connection)
            if connection.health_error is not None:
                raise connection.health_error
            connection.next_commit_error = self.health_commit_error
            return _Result(self.health_row if connection.alive else None)
        raise AssertionError(f"Unexpected SQL: {compact_sql}")


class _Connection:
    def __init__(self, database: _Database, *, pool: _Pool | None = None) -> None:
        self.pool = pool
        self.database = database
        self.checked_out = False
        self.commits = 0
        self.alive = True
        self.closed = False
        self.close_calls = 0
        self.closed_while_checked_out = False
        self.in_transaction = False
        self.health_error: Exception | None = None
        self.next_commit_error: Exception | None = None

    def execute(self, sql: str, params: tuple | None = None) -> _Result:
        if self.closed:
            raise ConnectionError("fake connection is closed")
        self.in_transaction = True
        return self.database.execute(self, sql, params)

    def commit(self) -> None:
        self.commits += 1
        if self.closed:
            raise ConnectionError("fake connection is closed")
        if self.next_commit_error is not None:
            error = self.next_commit_error
            self.next_commit_error = None
            raise error
        self.in_transaction = False

    def close(self) -> None:
        self.close_calls += 1
        self.closed_while_checked_out = self.checked_out
        self.checked_out = False
        self.closed = True
        self.alive = False
        self.in_transaction = False
        with self.database._lock:
            held_by_session = [
                key for key, holder in self.database.held_locks.items() if holder is self
            ]
            for key in held_by_session:
                del self.database.held_locks[key]


class _ConnectionContext:
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool
        self.connection: _Connection | None = None

    def __enter__(self) -> _Connection:
        self.connection = self.pool.checkout()
        return self.connection

    def __exit__(self, *_args) -> bool:
        assert self.connection is not None
        if not self.connection.closed:
            self.connection.in_transaction = False
        self.pool.return_connection(self.connection)
        return False


class _Pool:
    def __init__(self, database: _Database, *, max_size: int) -> None:
        self.database = database
        self.max_size = max_size
        self.connections: list[_Connection] = []
        self.available: list[_Connection] = []
        self.open_calls = 0
        self.close_calls = 0
        self.connection_error: Exception | None = None
        self._lock = Lock()

    def open(self) -> None:
        self.open_calls += 1

    def connection(self) -> _ConnectionContext:
        return _ConnectionContext(self)

    def checkout(self) -> _Connection:
        if self.connection_error is not None:
            raise self.connection_error
        with self._lock:
            if self.available:
                connection = self.available.pop()
            elif sum(not connection.closed for connection in self.connections) < self.max_size:
                connection = _Connection(self.database, pool=self)
                self.connections.append(connection)
            else:
                raise TimeoutError("fake pool exhausted")
            connection.checked_out = True
            return connection

    def return_connection(self, connection: _Connection) -> None:
        with self._lock:
            connection.checked_out = False
            if not connection.closed:
                self.available.append(connection)

    def close(self) -> None:
        self.close_calls += 1


class _LockConnectionFactory:
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.calls: list[str] = []
        self.connections: list[_Connection] = []
        self._lock = Lock()

    def __call__(self, dsn: str) -> _Connection:
        connection = _Connection(self.database)
        connection.checked_out = True
        with self._lock:
            self.calls.append(dsn)
            self.connections.append(connection)
        return connection


class _PoolFactory:
    def __init__(self, database: _Database | None = None) -> None:
        self.database = database or _Database()
        self.lock_factory = _LockConnectionFactory(self.database)
        self.calls: list[tuple[str, dict]] = []
        self.pools: list[_Pool] = []

    def __call__(self, dsn: str, **kwargs) -> _Pool:
        self.calls.append((dsn, kwargs))
        pool = _Pool(self.database, max_size=kwargs["max_size"])
        self.pools.append(pool)
        return pool


def _store(factory: _PoolFactory, **kwargs) -> SubtaskOperationStore:
    return SubtaskOperationStore(
        "postgresql://ledger",
        pool_factory=factory,
        lock_connection_factory=factory.lock_factory,
        **kwargs,
    )


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
    store = _store(factory, min_size=2, max_size=7)

    assert factory.calls == []
    assert store.load("missing") is None

    assert factory.calls == [
        ("postgresql://ledger", {"min_size": 2, "max_size": 7, "open": False})
    ]
    assert factory.pools[0].open_calls == 1
    assert factory.database.schema_calls == 1


def test_schema_installation_is_once_under_concurrent_loads() -> None:
    factory = _PoolFactory()
    store = _store(factory)
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
    store = _store(factory)
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


def test_nested_json_values_do_not_alias_inputs_cursor_rows_or_durable_state() -> None:
    factory = _PoolFactory()
    store = _store(factory)
    operation = _operation(
        request_payload={"nested": {"items": []}},
        state={"nested": {"items": []}, "revision": 0},
    )

    inserted = store.insert(operation)
    insert_params = factory.database.last_insert_params
    insert_result_row = factory.database.last_result_row
    assert insert_params is not None
    assert insert_result_row is not None

    operation.request_payload["nested"]["items"].append("caller")
    operation.state["nested"]["items"].append("caller")
    inserted.request_payload["nested"]["items"].append("returned")
    inserted.state["nested"]["items"].append("returned")

    assert insert_params[7].obj["nested"]["items"] == []
    assert insert_params[8].obj["nested"]["items"] == []
    assert insert_result_row[7]["nested"]["items"] == []
    assert insert_result_row[8]["nested"]["items"] == []
    loaded = store.load(operation.idempotency_key)
    assert loaded is not None
    assert loaded.request_payload["nested"]["items"] == []
    assert loaded.state["nested"]["items"] == []

    load_result_row = factory.database.last_result_row
    assert load_result_row is not None
    loaded.request_payload["nested"]["items"].append("loaded")
    loaded.state["nested"]["items"].append("loaded")
    assert load_result_row[7]["nested"]["items"] == []
    assert load_result_row[8]["nested"]["items"] == []

    candidate = replace(
        store.load(operation.idempotency_key),
        state={"nested": {"items": []}, "revision": 0},
    )
    checkpointed = store.checkpoint(candidate, expected_revision=0)
    checkpoint_params = factory.database.last_checkpoint_params
    checkpoint_result_row = factory.database.last_result_row
    assert checkpoint_params is not None
    assert checkpoint_result_row is not None

    candidate.state["nested"]["items"].append("caller")
    checkpointed.state["nested"]["items"].append("returned")
    assert checkpoint_params[0].obj["nested"]["items"] == []
    assert checkpoint_result_row[8]["nested"]["items"] == []
    reloaded = store.load(operation.idempotency_key)
    assert reloaded is not None
    assert reloaded.state["nested"]["items"] == []


def test_checkpoint_increments_revision_without_mutating_input_or_identity() -> None:
    factory = _PoolFactory()
    store = _store(factory)
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
    store = _store(factory)
    operation = store.insert(_operation())
    store.checkpoint(operation, expected_revision=0)

    with pytest.raises(OperationConflictError):
        store.checkpoint(operation, expected_revision=0)


def test_parent_lock_holds_same_connection_and_unlocks_in_finally() -> None:
    factory = _PoolFactory()
    store = _store(factory)

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
    assert connection.closed is True


def test_direct_lock_connection_opens_only_when_context_is_entered() -> None:
    factory = _PoolFactory()
    store = _store(factory)

    parent_context = store.try_parent_lock("yougile", "parent-1")
    assert factory.lock_factory.calls == []
    assert factory.pools == []

    with parent_context as parent_lock:
        assert parent_lock is not None
        assert factory.lock_factory.calls == ["postgresql://ledger"]

    assert factory.lock_factory.connections[0].closed is True


def test_default_lock_connector_honors_module_guard(monkeypatch) -> None:
    factory = _PoolFactory()
    monkeypatch.setattr(subtask_store_module.psycopg, "connect", factory.lock_factory)
    store = SubtaskOperationStore("postgresql://ledger", pool_factory=factory)

    assert store._lock_connection_factory is factory.lock_factory
    with store.try_parent_lock("yougile", "parent-1") as parent_lock:
        assert parent_lock is not None

    assert factory.lock_factory.calls == ["postgresql://ledger"]
    assert factory.lock_factory.connections[0].closed is True


def test_contended_parent_lock_yields_none_without_unlock() -> None:
    factory = _PoolFactory()
    factory.database.lock_results["parent-busy"] = False
    store = _store(factory)

    with store.try_parent_lock("yougile", "parent-busy") as parent_lock:
        assert parent_lock is None

    assert factory.database.unlock_calls == []
    connection = factory.database.lock_calls[0][0]
    assert connection.commits == 1
    assert connection.checked_out is False
    assert connection.closed is True

    factory.database.lock_results["parent-busy"] = True
    with store.try_parent_lock("yougile", "parent-busy") as parent_lock:
        assert parent_lock is not None
        assert factory.database.lock_calls[-1][0] is not connection
    assert len(factory.lock_factory.connections) == 2
    assert all(item.closed for item in factory.lock_factory.connections)


def test_parent_lock_does_not_starve_max_size_one_ledger_pool() -> None:
    factory = _PoolFactory()
    store = _store(factory, min_size=1, max_size=1)
    inserted = store.insert(_operation())

    with store.try_parent_lock("yougile", "parent-1") as parent_lock:
        assert parent_lock is not None
        loaded = store.load(inserted.idempotency_key)
        assert loaded is not None
        checkpointed = store.checkpoint(loaded, expected_revision=0)
        assert checkpointed.revision == 1

    assert len(factory.pools) == 1
    ledger_pool = factory.pools[0]
    lock_connection = factory.database.lock_calls[0][0]
    assert lock_connection.pool is None
    assert factory.database.schema_connections[0].pool is ledger_pool
    assert factory.database.schema_calls == 1
    assert len(ledger_pool.connections) == 1
    assert len(factory.lock_factory.connections) == 1
    assert ledger_pool.connections[0].in_transaction is False
    assert lock_connection.in_transaction is False
    assert lock_connection.closed is True


def test_same_parent_concurrent_direct_session_reaches_contention_immediately() -> None:
    pool_factory = _PoolFactory()
    lock_factory = _LockConnectionFactory(pool_factory.database)
    store = SubtaskOperationStore(
        "postgresql://ledger",
        max_size=1,
        pool_factory=pool_factory,
        lock_connection_factory=lock_factory,
    )
    first_entered = Event()
    release_first = Event()

    def hold_first() -> None:
        with store.try_parent_lock("yougile", "parent-1") as parent_lock:
            assert parent_lock is not None
            first_entered.set()
            assert release_first.wait(timeout=2)

    def contend_second():
        assert first_entered.wait(timeout=2)
        with store.try_parent_lock("yougile", "parent-1") as parent_lock:
            return parent_lock

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(hold_first)
        second = executor.submit(contend_second)
        assert second.result(timeout=1) is None
        assert first.done() is False
        release_first.set()
        first.result(timeout=2)

    assert len(lock_factory.connections) == 2
    assert len(pool_factory.database.lock_calls) == 2
    assert all(connection.closed for connection in lock_factory.connections)


def test_lock_first_does_not_initialize_ledger_schema_or_pool() -> None:
    factory = _PoolFactory()
    store = _store(factory)

    with store.try_parent_lock("yougile", "parent-1"):
        assert factory.database.schema_calls == 0

    assert factory.pools == []
    lock_connection = factory.lock_factory.connections[0]
    assert lock_connection.closed is True
    assert store.load("missing") is None
    ledger_pool = factory.pools[0]
    assert factory.database.schema_connections == [ledger_pool.connections[0]]
    assert lock_connection.pool is None


def test_acquired_parent_lock_commit_failure_attempts_unlock_and_propagates() -> None:
    factory = _PoolFactory()
    commit_error = RuntimeError("acquisition commit failed")
    factory.database.acquire_commit_error = commit_error
    store = _store(factory)

    with pytest.raises(RuntimeError) as exc_info:
        _enter_parent_lock(store)

    assert exc_info.value is commit_error
    connection = factory.database.lock_calls[0][0]
    assert factory.database.unlock_calls == [(connection, ("yougile", "parent-1"))]
    assert connection.commits == 2
    assert connection.closed is True
    assert connection.closed_while_checked_out is True
    assert connection.checked_out is False


def test_acquisition_error_remains_primary_when_unlock_also_fails() -> None:
    factory = _PoolFactory()
    commit_error = RuntimeError("acquisition commit failed")
    factory.database.acquire_commit_error = commit_error
    factory.database.unlock_error = RuntimeError("unlock failed")
    store = _store(factory)

    with pytest.raises(RuntimeError) as exc_info:
        _enter_parent_lock(store)

    assert exc_info.value is commit_error
    connection = factory.database.lock_calls[0][0]
    assert factory.database.unlock_calls == [(connection, ("yougile", "parent-1"))]
    assert connection.checked_out is False


def test_acquisition_execute_failure_discards_lock_connection() -> None:
    factory = _PoolFactory()
    acquire_error = RuntimeError("acquire failed indeterminately")
    factory.database.acquire_error = acquire_error
    store = _store(factory)

    with pytest.raises(RuntimeError) as exc_info:
        _enter_parent_lock(store)

    assert exc_info.value is acquire_error
    connection = factory.lock_factory.connections[0]
    assert connection.close_calls == 1
    assert connection.closed is True
    assert connection.closed_while_checked_out is True


@pytest.mark.parametrize("row", [None, (), (None,), (True, False)])
def test_malformed_acquisition_result_discards_lock_connection(row: tuple | None) -> None:
    factory = _PoolFactory()
    factory.database.lock_rows["parent-1"] = row
    store = _store(factory)

    with pytest.raises(LedgerUnavailableError):
        _enter_parent_lock(store)

    connection = factory.database.lock_calls[0][0]
    assert connection.close_calls == 1
    assert connection.closed is True
    assert connection.closed_while_checked_out is True


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
    store = _store(factory)

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
    store = _store(factory)
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
    store = _store(factory)

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
    store = _store(factory)
    disconnect = OSError("connection lost")

    with pytest.raises(LedgerUnavailableError) as exc_info, store.try_parent_lock(
        "yougile", "parent-1"
    ) as parent_lock:
        assert parent_lock is not None
        parent_lock._connection.health_error = disconnect
        parent_lock.ensure_alive()

    assert exc_info.value.__cause__ is disconnect
    connection = factory.database.lock_calls[0][0]
    assert connection.closed is True
    assert connection.closed_while_checked_out is True


def test_parent_lock_ensure_alive_commits_health_query_on_same_connection() -> None:
    factory = _PoolFactory()
    store = _store(factory)

    with store.try_parent_lock("yougile", "parent-1") as parent_lock:
        assert parent_lock is not None
        connection = factory.database.lock_calls[0][0]

        parent_lock.ensure_alive()

        assert factory.database.health_calls == [connection]
        assert connection.commits == 2
        assert connection.in_transaction is False
        assert connection.closed is False


def test_parent_lock_ensure_alive_commit_failure_discards_with_cause() -> None:
    factory = _PoolFactory()
    commit_error = RuntimeError("health commit failed")
    factory.database.health_commit_error = commit_error
    store = _store(factory)

    with pytest.raises(LedgerUnavailableError) as exc_info, store.try_parent_lock(
        "yougile", "parent-1"
    ) as parent_lock:
        assert parent_lock is not None
        parent_lock.ensure_alive()

    assert exc_info.value.__cause__ is commit_error
    connection = factory.database.lock_calls[0][0]
    assert factory.database.health_calls == [connection]
    assert connection.closed is True
    assert connection.closed_while_checked_out is True


def test_parent_lock_ensure_alive_rejects_malformed_result_and_discards() -> None:
    factory = _PoolFactory()
    factory.database.health_row = (1, 2)
    store = _store(factory)

    with pytest.raises(LedgerUnavailableError) as exc_info, store.try_parent_lock(
        "yougile", "parent-1"
    ) as parent_lock:
        assert parent_lock is not None
        parent_lock.ensure_alive()

    assert isinstance(exc_info.value.__cause__, ConnectionError)
    connection = factory.database.lock_calls[0][0]
    assert connection.closed is True
    assert connection.closed_while_checked_out is True


@pytest.mark.parametrize("failure", ["schema", "load", "acquire", "unlock", "connection"])
def test_database_and_lock_errors_propagate(failure: str) -> None:
    factory = _PoolFactory()
    store = _store(factory)
    expected = RuntimeError(f"{failure} failed")

    if failure == "schema":
        factory.database.schema_error = expected
        call = partial(store.load, "missing")
    elif failure == "load":
        factory.database.load_error = expected
        call = partial(store.load, "missing")
    elif failure == "acquire":
        factory.database.acquire_error = expected
        call = partial(_enter_parent_lock, store)
    elif failure == "unlock":
        factory.database.unlock_error = expected
        call = partial(_enter_parent_lock, store)
    else:
        store.load("missing")
        factory.pools[0].connection_error = expected
        call = partial(store.load, "missing")

    with pytest.raises(RuntimeError) as exc_info:
        call()
    assert exc_info.value is expected


def _enter_parent_lock(store: SubtaskOperationStore) -> None:
    with store.try_parent_lock("yougile", "parent-1"):
        pass


def test_close_resets_pool_and_schema_for_safe_reuse() -> None:
    factory = _PoolFactory()
    store = _store(factory)

    assert store.load("missing") is None
    first_pool = factory.pools[0]
    store.close()
    assert first_pool.close_calls == 1

    assert store.load("missing") is None
    assert len(factory.pools) == 2
    assert factory.pools[1].open_calls == 1
    assert factory.database.schema_calls == 2


def test_close_resets_ledger_pool_while_direct_lock_sessions_are_already_closed() -> None:
    factory = _PoolFactory()
    store = _store(factory)

    assert store.load("missing") is None
    with store.try_parent_lock("yougile", "parent-1"):
        pass
    ledger_pool = factory.pools[0]
    first_lock_connection = factory.lock_factory.connections[0]
    assert first_lock_connection.closed is True

    store.close()

    assert ledger_pool.close_calls == 1
    assert first_lock_connection.close_calls == 1
    assert store.load("missing") is None
    with store.try_parent_lock("yougile", "parent-1"):
        pass
    assert len(factory.pools) == 2
    assert len(factory.lock_factory.connections) == 2
    assert all(connection.closed for connection in factory.lock_factory.connections)
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
