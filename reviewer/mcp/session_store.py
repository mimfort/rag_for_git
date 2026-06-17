"""Персистентное хранилище подготовленных сессий ревью (Postgres).

Подложка для in-memory ``MCPReviewService._sessions``: переживает рестарт/краш
процесса reviewer-mcp между ``prepare_review`` и ``publish_review`` одного PR.
Использует ту же БД (PG_DSN), что и ChunkStore/ReviewHistory, но отдельную
таблицу ``review_sessions``. Все операции fail-soft — персист никогда не должен
ронять основной путь ревью.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

_SCHEMA = Path(__file__).with_name("session_store.sql").read_text()


class SessionStore:
    """Сохраняет/восстанавливает сериализованный PreparedReview по ключу (repo, pr).

    Пул соединений создаётся лениво; схема инициализируется идемпотентно при
    первом обращении. TTL применяется на чтении через условие ``WHERE``.
    """

    def __init__(self, pg_dsn: str, *, min_size: int = 1, max_size: int = 4) -> None:
        self.pg_dsn = pg_dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: ConnectionPool | None = None
        self._init_lock = threading.Lock()
        self._schema_ready = False

    def _ensure_pool(self) -> ConnectionPool:
        """Создать и открыть пул при первом обращении (thread-safe)."""
        if self._pool is None:
            with self._init_lock:
                if self._pool is None:
                    pool = ConnectionPool(
                        self.pg_dsn,
                        min_size=self._min_size,
                        max_size=self._max_size,
                        open=False,
                    )
                    pool.open()
                    self._pool = pool
        return self._pool

    def init_schema(self) -> None:
        """Создать таблицу review_sessions, если её нет (идемпотентно)."""
        with self._ensure_pool().connection() as conn:
            conn.execute(_SCHEMA)
            conn.commit()
        self._schema_ready = True

    def _connect(self):
        """Вернуть соединение из пула, гарантировав наличие схемы."""
        if not self._schema_ready:
            self.init_schema()
        return self._ensure_pool().connection()

    def close(self) -> None:
        """Закрыть пул соединений, если он был создан."""
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def save(self, repo: str, pr: int, payload: dict) -> None:
        """Upsert сериализованной сессии. Fail-soft: сбой только логируется."""
        sql = """
        INSERT INTO review_sessions (repo, pr_number, payload, created_at)
        VALUES (%s, %s, %s::jsonb, now())
        ON CONFLICT (repo, pr_number)
        DO UPDATE SET payload = EXCLUDED.payload, created_at = now()
        """
        try:
            with self._connect() as conn:
                conn.execute(sql, (repo, pr, json.dumps(payload, ensure_ascii=False)))
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось сохранить сессию %s#%s: %s", repo, pr, exc)

    def load(self, repo: str, pr: int, ttl_hours: int) -> dict | None:
        """Прочитать payload, если строка существует и не истёк TTL; иначе None.

        Fail-soft: при сбое БД возвращает None (вызывающий трактует как промах).
        """
        sql = """
        SELECT payload FROM review_sessions
        WHERE repo = %s AND pr_number = %s
          AND created_at > now() - make_interval(hours => %s)
        """
        try:
            with self._connect() as conn:
                row = conn.execute(sql, (repo, pr, ttl_hours)).fetchone()
            return row[0] if row else None
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось загрузить сессию %s#%s: %s", repo, pr, exc)
            return None

    def delete(self, repo: str, pr: int) -> None:
        """Удалить строку сессии. Fail-soft: сбой только логируется."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM review_sessions WHERE repo = %s AND pr_number = %s",
                    (repo, pr),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось удалить сессию %s#%s: %s", repo, pr, exc)
