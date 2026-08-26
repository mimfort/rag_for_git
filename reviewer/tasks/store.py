"""Хранилище задач доски в Postgres: эмбеддинги (pgvector) + BM25 (pg_search), RRF.

Отдельная таблица ``tasks`` (не code-``chunks``): у задач нет path/symbol/lines и
base/overlay-freshness. Зеркалит паттерн :class:`ChunkStore` — ленивый пул,
``register_vector`` на каждое соединение.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field

from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from reviewer.rrf import RRF_K

_BM25_STRIP = re.compile(r"[^\w\s]")
_LINKS_MIGRATION_SQL = (
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS links "
    "jsonb NOT NULL DEFAULT '[]'"
)


def _bm25_query(text: str) -> str:
    cleaned = _BM25_STRIP.sub(" ", text).strip()
    return cleaned or "____nomatch____"


def build_task_text(title: str | None, description: str | None,
                    criteria: list[str] | None, attachments: list[dict] | None = None,
                    *, embed_chars: int = 8000) -> str:
    """Текст задачи для эмбеддинга и BM25: заголовок + описание + критерии + вложения.

    Текст каждого вложения с непустым ``content_text`` обрезается до ``embed_chars``
    (усечение, не summary — синк не тратит LLM-токены)."""
    parts = [title or "", description or ""]
    if criteria:
        parts.append("\n".join(c for c in criteria if c))
    for att in attachments or []:
        text = (att.get("content_text") or "").strip()
        if text:
            parts.append(f"{att.get('name', '')}\n{text[:embed_chars]}")
    return "\n\n".join(p for p in parts if p).strip()


def task_content_hash(text: str) -> str:
    """Хэш нормализованного текста задачи (как Chunk.content_hash) — дедуп переэмбеда."""
    norm = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


@dataclass
class TaskRow:
    key: str
    aliases: list[str]
    title: str
    description: str
    status: str | None
    url: str | None
    content_hash: str
    text: str
    embedding: list[float]
    project: str = ""
    attachments: list[dict] = field(default_factory=list)
    links: list[dict] | None = None


@dataclass
class TaskHit:
    key: str
    title: str
    status: str | None
    score: float
    aliases: list[str] = field(default_factory=list)


class TaskStore:
    """Хранилище задач в Postgres (таблица ``tasks``). Ленивый пул, register_vector."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 4) -> None:
        self.dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: ConnectionPool | None = None
        self._init_lock = threading.Lock()
        self._links_schema_lock = threading.Lock()
        self._links_schema_ready = False

    def _ensure_pool(self) -> ConnectionPool:
        """Создать и открыть пул при первом обращении (thread-safe)."""
        if self._pool is None:
            with self._init_lock:
                if self._pool is None:
                    self._pool = ConnectionPool(
                        self.dsn, min_size=self._min_size, max_size=self._max_size,
                        open=False, configure=lambda conn: register_vector(conn),
                    )
                    self._pool.open()
        return self._pool

    def _connect(self):
        """Вернуть контекстный менеджер соединения из пула."""
        return self._ensure_pool().connection()

    def _ensure_links_schema(self) -> None:
        """Лениво применить additive-миграцию links без рекурсии через _connect."""
        if self._links_schema_ready:
            return
        with self._links_schema_lock:
            if self._links_schema_ready:
                return
            with self._ensure_pool().connection() as conn:
                conn.execute(_LINKS_MIGRATION_SQL)
                conn.commit()
            self._links_schema_ready = True

    def _connect_links(self):
        """Соединение для запросов, читающих или записывающих tasks.links."""
        self._ensure_links_schema()
        return self._ensure_pool().connection()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None
        self._links_schema_ready = False

    def existing_hash(self, key: str) -> str | None:
        """content_hash уже проиндексированной задачи (None если её нет)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content_hash FROM tasks WHERE key = %s", (key,)
            ).fetchone()
        return row[0] if row else None

    def get_task(self, key: str, project: str | None = None) -> TaskRow | None:
        """Задача по ключу/алиасу; при project — только из этого проекта (PRI-170).

        Матч по каноническому ``key`` ИЛИ по ``aliases`` (стор ключует по ID-N, а
        вызов часто передаёт проектный PRI-N). Эмбеддинг не читается (не нужен для
        брифа) — в TaskRow ставится []. None, если задачи нет.
        """
        sql = ("SELECT key, aliases, title, description, status, url, "
               "content_hash, text, project, attachments, links FROM tasks "
               "WHERE (key = %s OR %s = ANY(aliases))")
        params: list = [key, key]
        if project:
            sql += " AND project = %s"
            params.append(project)
        sql += " LIMIT 1"
        with self._connect_links() as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return TaskRow(
            key=row[0], aliases=list(row[1] or []), title=row[2],
            description=row[3], status=row[4], url=row[5],
            content_hash=row[6], text=row[7], embedding=[], project=row[8],
            attachments=list(row[9] or []), links=list(row[10] or []))

    def upsert_task(self, row: TaskRow) -> None:
        sql = """
        INSERT INTO tasks (key, aliases, title, description, status, url,
                           content_hash, text, embedding, project, attachments, links)
        VALUES (%(key)s,%(aliases)s,%(title)s,%(description)s,%(status)s,%(url)s,
                %(content_hash)s,%(text)s,%(embedding)s,%(project)s,
                %(attachments)s::jsonb,%(links)s::jsonb)
        ON CONFLICT (key) DO UPDATE SET
            aliases=EXCLUDED.aliases, title=EXCLUDED.title,
            description=EXCLUDED.description, status=EXCLUDED.status,
            url=EXCLUDED.url, content_hash=EXCLUDED.content_hash,
            text=EXCLUDED.text, embedding=EXCLUDED.embedding, project=EXCLUDED.project,
            attachments=EXCLUDED.attachments,
            links=CASE WHEN %(links_supplied)s THEN EXCLUDED.links ELSE tasks.links END
        """
        params = {
            "key": row.key, "aliases": row.aliases, "title": row.title,
            "description": row.description, "status": row.status, "url": row.url,
            "content_hash": row.content_hash, "text": row.text,
            "embedding": row.embedding, "project": row.project,
            "attachments": json.dumps(row.attachments, ensure_ascii=False),
            "links": json.dumps(row.links if row.links is not None else [],
                                ensure_ascii=False),
            "links_supplied": row.links is not None,
        }
        with self._connect_links() as conn:
            conn.execute(sql, params)
            conn.commit()

    def update_links(self, key: str, links: list[dict]) -> bool:
        """Заменить сохранённый snapshot links; False, если задачи нет в сторе."""
        with self._connect_links() as conn:
            result = conn.execute(
                "UPDATE tasks SET links=%s::jsonb WHERE key=%s RETURNING 1",
                (json.dumps(links, ensure_ascii=False), key),
            )
            updated = result.fetchone() is not None
            conn.commit()
        return updated

    def update_meta(self, key: str, title: str, status: str | None,
                    url: str | None, aliases: list[str], project: str = "") -> None:
        """Обновить лёгкие метаданные без переэмбеда (когда content_hash совпал)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET title=%s, status=%s, url=%s, aliases=%s, project=%s "
                "WHERE key=%s",
                (title, status, url, aliases, project, key),
            )
            conn.commit()

    def update_meta_batch(self, metas: list[dict]) -> None:
        """Батч-обновление плоских метаданных (PRI-207 meta-refresh): один
        executemany-UPDATE. Задача не в сторе → 0 строк (no-op, не создаёт
        неполных строк, не трогает embedding). Пустой батч → no-op."""
        rows = [(m.get("title") or "", m.get("status"), m.get("url"),
                 m.get("aliases") or [], m.get("project") or "", m["key"])
                for m in metas if isinstance(m, dict) and m.get("key")]
        if not rows:
            return
        with self._connect() as conn, conn.cursor() as cur:
            cur.executemany(
                "UPDATE tasks SET title=%s, status=%s, url=%s, aliases=%s, "
                "project=%s WHERE key=%s",
                rows,
            )
            conn.commit()

    def list_keys(self, project: str | None = None) -> list[str]:
        """Ключи задач; при project — только этого проекта (для scoped purge)."""
        sql = "SELECT key FROM tasks"
        params: list = []
        if project:
            sql += " WHERE project = %s"
            params.append(project)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [r[0] for r in rows]

    def delete_tasks(self, keys: list[str]) -> int:
        """Удалить задачи по ключам. Возвращает кол-во удалённых строк."""
        if not keys:
            return 0
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM tasks WHERE key = ANY(%s)", (keys,)
            )
            conn.commit()
        return result.rowcount

    def search(self, query_text: str, query_embedding: list[float],
               top_k: int = 5, candidates: int = 50,
               project: str | None = None) -> list[TaskHit]:
        """Гибрид RRF (BM25 ⊕ ANN). При project — скоуп по проекту (PRI-170)."""
        proj = "AND project = %(project)s" if project else ""
        sql = f"""
        WITH bm25 AS (
            SELECT id, RANK() OVER (ORDER BY pdb.score(id) DESC) AS rank
            FROM tasks WHERE text @@@ %(q)s {proj}
            ORDER BY pdb.score(id) DESC LIMIT %(cand)s
        ),
        ann AS (
            SELECT id, RANK() OVER (ORDER BY embedding <=> %(vec)s) AS rank
            FROM tasks WHERE TRUE {proj}
            ORDER BY embedding <=> %(vec)s LIMIT %(cand)s
        ),
        rrf AS (
            SELECT id, 1.0/(%(rrf_k)s::int + rank) AS s FROM bm25
            UNION ALL SELECT id, 1.0/(%(rrf_k)s::int + rank) AS s FROM ann
        )
        SELECT t.key, t.title, t.status, t.aliases, SUM(r.s) AS score
        FROM rrf r JOIN tasks t USING (id)
        GROUP BY t.id, t.key, t.title, t.status, t.aliases
        ORDER BY score DESC LIMIT %(k)s
        """
        params = {"q": _bm25_query(query_text), "vec": Vector(query_embedding),
                  "cand": candidates, "k": top_k, "rrf_k": RRF_K}
        if project:
            params["project"] = project
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [TaskHit(key=k, title=t, status=s, score=float(sc), aliases=list(a or []))
                for (k, t, s, a, sc) in rows]
