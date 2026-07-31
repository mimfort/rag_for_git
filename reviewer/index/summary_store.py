# reviewer/index/summary_store.py
"""Хранилище предрасчитанных summary подсистем (таблица subsystem_summaries).

Зеркалит паттерн TaskStore: ленивый пул, register_vector на каждое соединение.
Таблицу создаёт ChunkStore.init_schema (общий schema.sql)."""
from __future__ import annotations

import threading
from datetime import datetime

import psycopg.errors
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool
from psycopg.types.json import Jsonb


_UPSERT_SUMMARY_SQL = """
INSERT INTO subsystem_summaries
    (repo, branch, cluster_key, title, summary, member_node_ids,
     source_hash, embedding, updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (repo, branch, cluster_key) DO UPDATE SET
    title=EXCLUDED.title, summary=EXCLUDED.summary,
    member_node_ids=EXCLUDED.member_node_ids,
    source_hash=EXCLUDED.source_hash,
    embedding = CASE
        WHEN %s AND subsystem_summaries.source_hash = EXCLUDED.source_hash
        THEN COALESCE(EXCLUDED.embedding, subsystem_summaries.embedding)
        ELSE EXCLUDED.embedding
    END,
    updated_at=now()
"""

_LOCK_SUMMARY_BRANCH_SQL = """
SELECT pg_advisory_xact_lock(
    hashtextextended(%s, hashtextextended(%s, 0))
)
"""


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
                       embedding: list[float] | None = None,
                       preserve_embedding: bool = True) -> None:
        """Idempotent upsert сводки.

        По умолчанию embedding=None сохраняет существующий вектор при неизменном
        source_hash. ``preserve_embedding=False`` атомарно сбрасывает его независимо
        от hash, чтобы новый текст не был доступен через старый ANN-вектор.
        """
        vec = Vector(embedding) if embedding is not None else None
        with self._connect() as conn:
            conn.execute(
                _UPSERT_SUMMARY_SQL,
                (repo, branch, cluster_key, title, summary,
                 member_node_ids, source_hash, vec, preserve_embedding),
            )
            conn.commit()

    def get_fragments(self, repo: str, branch: str) -> list[dict]:
        """Вернуть file-summary fragments ветки в детерминированном порядке."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cluster_key, path, fingerprint, summary, provenance, updated_at "
                    "FROM subsystem_summary_fragments "
                    "WHERE repo=%s AND branch=%s ORDER BY cluster_key, path",
                    (repo, branch),
                ).fetchall()
        except psycopg.errors.UndefinedTable:
            return []
        return [
            {
                "cluster_key": cluster_key,
                "path": path,
                "fingerprint": fingerprint,
                "summary": summary,
                "provenance": provenance,
                "updated_at": updated_at.isoformat(),
            }
            for cluster_key, path, fingerprint, summary, provenance, updated_at in rows
        ]

    def get_completed_depth(self, repo: str, branch: str) -> int | None:
        """Вернуть depth последнего полностью завершённого прохода."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT completed_depth FROM subsystem_summary_state "
                    "WHERE repo=%s AND branch=%s",
                    (repo, branch),
                ).fetchone()
        except psycopg.errors.UndefinedTable:
            return None
        return int(row[0]) if row is not None else None

    def get_completed_layout(self, repo: str, branch: str) -> str | None:
        """Вернуть token последнего полностью завершённого layout."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT completed_layout FROM subsystem_summary_state "
                    "WHERE repo=%s AND branch=%s",
                    (repo, branch),
                ).fetchone()
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            return None
        return str(row[0]) if row is not None and row[0] is not None else None

    @staticmethod
    def _validate_new_fragments(
        current_fingerprints: dict[str, str],
        new_fragments: list[dict],
    ) -> dict[str, dict]:
        """Проверить уникальность и актуальность сгенерированных fragments."""
        result: dict[str, dict] = {}
        for fragment in new_fragments:
            try:
                path = fragment["path"]
                fingerprint = fragment["fingerprint"]
                summary = fragment["summary"]
            except (KeyError, TypeError) as exc:
                raise ValueError("Неполный payload summary fragment") from exc
            if path in result:
                raise ValueError(f"Дублирующийся summary fragment: {path}")
            if current_fingerprints.get(path) != fingerprint:
                raise ValueError(f"Fingerprint summary fragment устарел: {path}")
            result[path] = {
                "path": path,
                "fingerprint": fingerprint,
                "summary": summary,
                "provenance": fragment.get("provenance", {}),
            }
        return result

    def commit_summary_bundle(
        self,
        repo: str,
        branch: str,
        cluster_key: str,
        title: str,
        summary: str,
        member_node_ids: list[str],
        source_hash: str,
        *,
        current_fingerprints: dict[str, str],
        new_fragments: list[dict],
    ) -> dict:
        """Атомарно сохранить fragments и итоговую сводку одного кластера.

        Существующий fragment переиспользуется только при точном совпадении
        fingerprint. Отсутствующее покрытие или неоднозначный перенос откатывают
        всю транзакцию вместе со сводкой.
        """
        incoming = self._validate_new_fragments(current_fingerprints, new_fragments)
        metrics = {"created": len(incoming), "reused": 0, "removed": 0, "moved": 0}
        with self._connect() as conn, conn.transaction():
            conn.execute(_LOCK_SUMMARY_BRANCH_SQL, (repo, branch))
            rows = conn.execute(
                "SELECT cluster_key, path, fingerprint, summary, provenance "
                "FROM subsystem_summary_fragments "
                "WHERE repo=%s AND branch=%s ORDER BY cluster_key, path FOR UPDATE",
                (repo, branch),
            ).fetchall()
            by_target = {
                path: (fingerprint, fragment_summary, provenance)
                for stored_cluster, path, fingerprint, fragment_summary, provenance in rows
                if stored_cluster == cluster_key
            }
            by_cross: dict[tuple[str, str], list[tuple[str, str, dict]]] = {}
            for stored_cluster, path, fingerprint, fragment_summary, provenance in rows:
                if stored_cluster != cluster_key:
                    by_cross.setdefault((path, fingerprint), []).append(
                        (stored_cluster, fragment_summary, provenance)
                    )

            removed = conn.execute(
                "DELETE FROM subsystem_summary_fragments "
                "WHERE repo=%s AND branch=%s AND cluster_key=%s "
                "AND NOT (path = ANY(%s))",
                (repo, branch, cluster_key, list(current_fingerprints)),
            )
            metrics["removed"] = removed.rowcount

            for path, fingerprint in sorted(current_fingerprints.items()):
                fragment = incoming.get(path)
                if fragment is not None:
                    fragment_summary = fragment["summary"]
                    provenance = fragment["provenance"]
                else:
                    target = by_target.get(path)
                    if target is not None and target[0] == fingerprint:
                        fragment_summary, provenance = target[1], target[2]
                        metrics["reused"] += 1
                    else:
                        candidates = by_cross.get((path, fingerprint), [])
                        if len(candidates) != 1:
                            raise ValueError(
                                f"Нет однозначного актуального summary fragment: {path}"
                            )
                        _source_cluster, fragment_summary, provenance = candidates[0]
                        metrics["moved"] += 1

                conn.execute(
                    "DELETE FROM subsystem_summary_fragments "
                    "WHERE repo=%s AND branch=%s AND path=%s AND cluster_key<>%s",
                    (repo, branch, path, cluster_key),
                )
                conn.execute(
                    "INSERT INTO subsystem_summary_fragments "
                    "(repo, branch, cluster_key, path, fingerprint, summary, "
                    " provenance, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,now()) "
                    "ON CONFLICT (repo, branch, cluster_key, path) DO UPDATE SET "
                    "fingerprint=EXCLUDED.fingerprint, summary=EXCLUDED.summary, "
                    "provenance=EXCLUDED.provenance, updated_at=now()",
                    (
                        repo,
                        branch,
                        cluster_key,
                        path,
                        fingerprint,
                        fragment_summary,
                        Jsonb(provenance),
                    ),
                )

            final_rows = conn.execute(
                "SELECT path, fingerprint FROM subsystem_summary_fragments "
                "WHERE repo=%s AND branch=%s AND cluster_key=%s ORDER BY path",
                (repo, branch, cluster_key),
            ).fetchall()
            if dict(final_rows) != current_fingerprints:
                raise ValueError("Итоговое покрытие summary fragments неполно")

            conn.execute(
                _UPSERT_SUMMARY_SQL,
                (
                    repo,
                    branch,
                    cluster_key,
                    title,
                    summary,
                    member_node_ids,
                    source_hash,
                    None,
                    False,
                ),
            )
        return metrics

    def prune_except_and_set_depth(
        self,
        repo: str,
        branch: str,
        keep_keys: list[str],
        depth: int,
    ) -> dict:
        """Атомарно удалить осиротевшие данные и отметить завершённый depth."""
        with self._connect() as conn, conn.transaction():
            conn.execute(_LOCK_SUMMARY_BRANCH_SQL, (repo, branch))
            fragments = conn.execute(
                "DELETE FROM subsystem_summary_fragments "
                "WHERE repo=%s AND branch=%s AND NOT (cluster_key = ANY(%s))",
                (repo, branch, list(keep_keys)),
            )
            summaries = conn.execute(
                "DELETE FROM subsystem_summaries "
                "WHERE repo=%s AND branch=%s AND NOT (cluster_key = ANY(%s))",
                (repo, branch, list(keep_keys)),
            )
            conn.execute(
                "INSERT INTO subsystem_summary_state "
                "(repo, branch, completed_depth, updated_at) VALUES (%s,%s,%s,now()) "
                "ON CONFLICT (repo, branch) DO UPDATE SET "
                "completed_depth=EXCLUDED.completed_depth, updated_at=now()",
                (repo, branch, depth),
            )
            return {
                "pruned": summaries.rowcount,
                "fragments_pruned": fragments.rowcount,
                "depth": depth,
            }

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
                    "SELECT cluster_key, title, summary, source_hash, updated_at FROM subsystem_summaries "
                    "WHERE repo=%s AND branch=%s ORDER BY cluster_key",
                    (repo, branch)).fetchall()
        except psycopg.errors.UndefinedTable:
            return []
        return [{"cluster_key": k, "title": t, "summary": s, "source_hash": h,
                 "updated_at": u.isoformat()}
                for k, t, s, h, u in rows]

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
                    "SELECT cluster_key, title, summary, source_hash, updated_at "
                    "FROM subsystem_summaries "
                    "WHERE repo=%s AND branch=%s AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> %s LIMIT %s",
                    (repo, branch, Vector(query_embedding), top_k)).fetchall()
        except psycopg.errors.UndefinedTable:
            return []
        return [{"cluster_key": k, "title": t, "summary": s, "source_hash": h,
                 "updated_at": u.isoformat()}
                for k, t, s, h, u in rows]

    def count_summaries(self, repo: str, branch: str) -> int:
        """Число сводок repo/branch — для порога масштаба в query-пути (PRI-167)."""
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
        """Записать эмбеддинг одной сводки (бэкфилл NULL-векторов, PRI-167)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE subsystem_summaries SET embedding=%s "
                "WHERE repo=%s AND branch=%s AND cluster_key=%s",
                (Vector(embedding), repo, branch, cluster_key))
            conn.commit()

    def set_embedding_if_source_hash(
        self,
        repo: str,
        branch: str,
        cluster_key: str,
        source_hash: str,
        embedding: list[float],
        *,
        title: str | None = None,
        summary: str | None = None,
    ) -> bool:
        """Записать вектор, только если сводка не изменилась после генерации.

        Переданные вместе ``title``/``summary`` усиливают CAS для случая, когда
        конкурирующая запись сохранила тот же структурный source_hash с новым текстом.
        """
        if (title is None) != (summary is None):
            raise ValueError("title и summary для embedding CAS передаются вместе")
        query = (
            "UPDATE subsystem_summaries SET embedding=%s "
            "WHERE repo=%s AND branch=%s AND cluster_key=%s AND source_hash=%s"
        )
        params: tuple = (Vector(embedding), repo, branch, cluster_key, source_hash)
        if title is not None:
            query += " AND title=%s AND summary=%s"
            params += (title, summary)
        with self._connect() as conn:
            cur = conn.execute(query, params)
            conn.commit()
            return cur.rowcount == 1
