# reviewer/index/summary_store.py
"""Хранилище предрасчитанных summary подсистем (таблица subsystem_summaries).

Зеркалит паттерн TaskStore: ленивый пул, register_vector на каждое соединение.
Таблицу создаёт ChunkStore.init_schema (общий schema.sql)."""
from __future__ import annotations

import threading

import psycopg.errors
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool


class SummaryStore:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 4) -> None:
        self.dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: ConnectionPool | None = None
        self._init_lock = threading.Lock()

    def _ensure_pool(self) -> ConnectionPool:
        if self._pool is None:
            with self._init_lock:
                if self._pool is None:
                    self._pool = ConnectionPool(
                        self.dsn, min_size=self._min_size, max_size=self._max_size,
                        open=False, configure=lambda conn: register_vector(conn))
                    self._pool.open()
        return self._pool

    def _connect(self):
        return self._ensure_pool().connection()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def upsert_summary(self, repo: str, branch: str, cluster_key: str, title: str,
                       summary: str, member_node_ids: list[str], source_hash: str) -> None:
        sql = """
        INSERT INTO subsystem_summaries
            (repo, branch, cluster_key, title, summary, member_node_ids, source_hash, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (repo, branch, cluster_key) DO UPDATE SET
            title=EXCLUDED.title, summary=EXCLUDED.summary,
            member_node_ids=EXCLUDED.member_node_ids,
            source_hash=EXCLUDED.source_hash, updated_at=now()
        """
        with self._connect() as conn:
            conn.execute(sql, (repo, branch, cluster_key, title, summary,
                               member_node_ids, source_hash))
            conn.commit()

    def get_source_hashes(self, repo: str, branch: str) -> dict[str, str]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cluster_key, source_hash FROM subsystem_summaries "
                    "WHERE repo=%s AND branch=%s", (repo, branch)).fetchall()
        except psycopg.errors.UndefinedTable:
            return {}
        return {k: h for k, h in rows}

    def get_summaries(self, repo: str, branch: str) -> list[dict]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cluster_key, title, summary, updated_at FROM subsystem_summaries "
                    "WHERE repo=%s AND branch=%s ORDER BY cluster_key",
                    (repo, branch)).fetchall()
        except psycopg.errors.UndefinedTable:
            return []
        return [{"cluster_key": k, "title": t, "summary": s, "updated_at": u.isoformat()}
                for k, t, s, u in rows]

    def get_summary(self, repo: str, branch: str, cluster_key: str) -> dict | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT cluster_key, title, summary, member_node_ids, source_hash, updated_at "
                    "FROM subsystem_summaries WHERE repo=%s AND branch=%s AND cluster_key=%s",
                    (repo, branch, cluster_key)).fetchone()
        except psycopg.errors.UndefinedTable:
            return None
        if row is None:
            return None
        return {"cluster_key": row[0], "title": row[1], "summary": row[2],
                "member_node_ids": list(row[3] or []), "source_hash": row[4],
                "updated_at": row[5].isoformat()}
