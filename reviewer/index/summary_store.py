# reviewer/index/summary_store.py
"""Хранилище предрасчитанных summary подсистем (таблица subsystem_summaries).

Зеркалит паттерн TaskStore: ленивый пул, register_vector на каждое соединение.
Таблицу создаёт ChunkStore.init_schema (общий schema.sql)."""
from __future__ import annotations

import threading
from datetime import datetime

import psycopg.errors
from pgvector.psycopg import Vector, register_vector
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
                       summary: str, member_node_ids: list[str], source_hash: str,
                       embedding: list[float] | None = None) -> None:
        """Idempotent upsert сводки. embedding=None сохраняет существующий вектор
        (COALESCE) — дедуп по source_hash на стороне вызывающего: при неизменном
        хеше эмбеддинг не пересчитывается и передаётся None (PRI-167)."""
        sql = """
        INSERT INTO subsystem_summaries
            (repo, branch, cluster_key, title, summary, member_node_ids,
             source_hash, embedding, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (repo, branch, cluster_key) DO UPDATE SET
            title=EXCLUDED.title, summary=EXCLUDED.summary,
            member_node_ids=EXCLUDED.member_node_ids,
            source_hash=EXCLUDED.source_hash,
            embedding=COALESCE(EXCLUDED.embedding, subsystem_summaries.embedding),
            updated_at=now()
        """
        vec = Vector(embedding) if embedding is not None else None
        with self._connect() as conn:
            conn.execute(sql, (repo, branch, cluster_key, title, summary,
                               member_node_ids, source_hash, vec))
            conn.commit()

    def delete_summaries_except(self, repo: str, branch: str, keep_keys: list[str]) -> int:
        """Удалить сводки repo/branch, чей cluster_key НЕ в keep_keys; вернуть число удалённых.

        Пустой keep_keys → удаляет все сводки repo/branch (вызывающий гейтит на непустой base).
        Используется prune_subsystem_summaries для чистки осиротевших при смене depth сводок."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM subsystem_summaries "
                "WHERE repo=%s AND branch=%s AND NOT (cluster_key = ANY(%s))",
                (repo, branch, list(keep_keys)))
            conn.commit()
            return cur.rowcount

    def get_source_hashes(self, repo: str, branch: str) -> dict[str, str]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cluster_key, source_hash FROM subsystem_summaries "
                    "WHERE repo=%s AND branch=%s", (repo, branch)).fetchall()
        except psycopg.errors.UndefinedTable:
            return {}
        return {k: h for k, h in rows}

    def get_updated_ats(self, repo: str, branch: str) -> dict[str, datetime]:
        """updated_at по cluster_key — для упорядочивания stale-кластеров под cap (PRI-165:
        без сводки → старейшие первыми). Нет таблицы → {} (fail-soft, как get_source_hashes)."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cluster_key, updated_at FROM subsystem_summaries "
                    "WHERE repo=%s AND branch=%s", (repo, branch)).fetchall()
        except psycopg.errors.UndefinedTable:
            return {}
        return {k: u for k, u in rows}

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

    def search_summaries(self, repo: str, branch: str, query_embedding: list[float],
                         top_k: int) -> list[dict]:
        """ANN-поиск (cosine) по сводкам с эмбеддингом — приор по близости (PRI-167)."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cluster_key, title, summary, updated_at "
                    "FROM subsystem_summaries "
                    "WHERE repo=%s AND branch=%s AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> %s LIMIT %s",
                    (repo, branch, Vector(query_embedding), top_k)).fetchall()
        except psycopg.errors.UndefinedTable:
            return []
        return [{"cluster_key": k, "title": t, "summary": s, "updated_at": u.isoformat()}
                for k, t, s, u in rows]

    def count_summaries(self, repo: str, branch: str) -> int:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM subsystem_summaries WHERE repo=%s AND branch=%s",
                    (repo, branch)).fetchone()
        except psycopg.errors.UndefinedTable:
            return 0
        return int(row[0]) if row else 0

    def get_pending_embeddings(self, repo: str, branch: str) -> list[dict]:
        """Сводки без эмбеддинга (embedding IS NULL) — для серверного бэкфилла (PRI-167)."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cluster_key, title, summary FROM subsystem_summaries "
                    "WHERE repo=%s AND branch=%s AND embedding IS NULL ORDER BY cluster_key",
                    (repo, branch)).fetchall()
        except psycopg.errors.UndefinedTable:
            return []
        return [{"cluster_key": k, "title": t, "summary": s} for k, t, s in rows]

    def set_embedding(self, repo: str, branch: str, cluster_key: str,
                      embedding: list[float]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE subsystem_summaries SET embedding=%s "
                "WHERE repo=%s AND branch=%s AND cluster_key=%s",
                (Vector(embedding), repo, branch, cluster_key))
            conn.commit()
