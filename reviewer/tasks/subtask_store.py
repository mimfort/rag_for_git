"""Durable ledger операций создания подзадач в Postgres."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

_SCHEMA = Path(__file__).with_name("subtask_store.sql").read_text(encoding="utf-8")
_COLUMNS = (
    "idempotency_key, board_type, parent_input_key, parent_task_id, "
    "source_board_id, source_column_id, request_hash, request_payload, "
    "state, status, created_at, updated_at"
)
_PARENT_LOCK_SQL = """
SELECT pg_try_advisory_lock(
    hashtextextended(%s, hashtextextended(%s, 0))
)
"""
_PARENT_UNLOCK_SQL = """
SELECT pg_advisory_unlock(
    hashtextextended(%s, hashtextextended(%s, 0))
)
"""


class OperationConflictError(RuntimeError):
    """Сохранённая ревизия операции изменилась до checkpoint."""


class LedgerUnavailableError(RuntimeError):
    """Ledger-соединение не может подтвердить безопасное продолжение операции."""


@dataclass(frozen=True)
class SubtaskOperation:
    """Сохранённое состояние идемпотентной операции над подзадачами."""

    idempotency_key: str
    board_type: str
    parent_input_key: str
    parent_task_id: str
    source_board_id: str
    source_column_id: str
    request_hash: str
    request_payload: dict
    state: dict
    status: str
    created_at: datetime | None
    updated_at: datetime | None

    @property
    def revision(self) -> int:
        return int(self.state.get("revision", 0))


def _operation_from_row(row: Sequence) -> SubtaskOperation:
    return SubtaskOperation(
        idempotency_key=row[0],
        board_type=row[1],
        parent_input_key=row[2],
        parent_task_id=row[3],
        source_board_id=row[4],
        source_column_id=row[5],
        request_hash=row[6],
        request_payload=dict(row[7]),
        state=dict(row[8]),
        status=row[9],
        created_at=row[10],
        updated_at=row[11],
    )


class ParentOperationLock:
    """Захваченная session-level блокировка на выделенном соединении."""

    def __init__(self, connection) -> None:
        self._connection = connection

    def ensure_alive(self) -> None:
        """Fail-closed проверка соединения непосредственно перед внешним POST."""
        try:
            row = self._connection.execute("SELECT 1").fetchone()
            if getattr(self._connection, "closed", False) or not row or row[0] != 1:
                raise ConnectionError("Ledger-соединение вернуло непригодный результат")
        except Exception as exc:
            raise LedgerUnavailableError("Ledger-соединение недоступно") from exc


class SubtaskOperationStore:
    """Durable ledger с optimistic checkpoint и блокировкой родительской задачи."""

    def __init__(
        self,
        pg_dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
        pool_factory: Callable[..., ConnectionPool] = ConnectionPool,
    ) -> None:
        self.pg_dsn = pg_dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool_factory = pool_factory
        self._pool: ConnectionPool | None = None
        self._init_lock = threading.Lock()
        self._schema_ready = False

    def _ensure_ready(self) -> ConnectionPool:
        pool = self._pool
        if pool is not None and self._schema_ready:
            return pool
        with self._init_lock:
            pool = self._pool
            if pool is None:
                pool = self._pool_factory(
                    self.pg_dsn,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    open=False,
                )
                pool.open()
                self._pool = pool
            if not self._schema_ready:
                with pool.connection() as conn:
                    conn.execute(_SCHEMA)
                    conn.commit()
                self._schema_ready = True
            return pool

    def _connect(self):
        return self._ensure_ready().connection()

    def close(self) -> None:
        """Закрыть текущий пул и разрешить повторную ленивую инициализацию."""
        with self._init_lock:
            pool = self._pool
            self._pool = None
            self._schema_ready = False
            if pool is not None:
                pool.close()

    def load(self, idempotency_key: str) -> SubtaskOperation | None:
        sql = f"SELECT {_COLUMNS} FROM subtask_operations WHERE idempotency_key = %s"
        with self._connect() as conn:
            row = conn.execute(sql, (idempotency_key,)).fetchone()
        return _operation_from_row(row) if row is not None else None

    def insert(self, operation: SubtaskOperation) -> SubtaskOperation:
        sql = f"""
        INSERT INTO subtask_operations (
            idempotency_key, board_type, parent_input_key, parent_task_id,
            source_board_id, source_column_id, request_hash, request_payload, state, status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
        RETURNING {_COLUMNS}
        """
        params = (
            operation.idempotency_key,
            operation.board_type,
            operation.parent_input_key,
            operation.parent_task_id,
            operation.source_board_id,
            operation.source_column_id,
            operation.request_hash,
            Jsonb(operation.request_payload),
            Jsonb(operation.state),
            operation.status,
        )
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            conn.commit()
        if row is None:
            raise LedgerUnavailableError("Postgres не вернул вставленную ledger-операцию")
        return _operation_from_row(row)

    def checkpoint(
        self,
        operation: SubtaskOperation,
        *,
        expected_revision: int,
    ) -> SubtaskOperation:
        state = dict(operation.state)
        state["revision"] = expected_revision + 1
        sql = f"""
        UPDATE subtask_operations
        SET state = %s::jsonb, status = %s, updated_at = now()
        WHERE idempotency_key = %s
          AND COALESCE((state ->> 'revision')::integer, 0) = %s
        RETURNING {_COLUMNS}
        """
        with self._connect() as conn:
            row = conn.execute(
                sql,
                (Jsonb(state), operation.status, operation.idempotency_key, expected_revision),
            ).fetchone()
            conn.commit()
        if row is None:
            raise OperationConflictError(
                f"Ledger-операция {operation.idempotency_key!r} имеет другую ревизию"
            )
        return _operation_from_row(row)

    @contextmanager
    def try_parent_lock(
        self,
        board_type: str,
        parent_task_id: str,
    ) -> Iterator[ParentOperationLock | None]:
        """Попытаться удержать session lock на одном соединении до выхода из context."""
        pool = self._ensure_ready()
        params = (board_type, parent_task_id)
        with pool.connection() as conn:
            row = conn.execute(_PARENT_LOCK_SQL, params).fetchone()
            conn.commit()
            if not row or not isinstance(row[0], bool):
                raise LedgerUnavailableError("Postgres не вернул результат захвата lock")
            if row[0] is False:
                yield None
                return

            try:
                yield ParentOperationLock(conn)
            finally:
                unlock_row = conn.execute(_PARENT_UNLOCK_SQL, params).fetchone()
                conn.commit()
                if unlock_row is None or not unlock_row or unlock_row[0] is not True:
                    raise LedgerUnavailableError("Postgres не подтвердил освобождение lock")
