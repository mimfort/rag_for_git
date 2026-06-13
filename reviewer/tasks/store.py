"""Хранилище задач доски в Postgres: эмбеддинги (pgvector) + BM25 (pg_search), RRF.

Отдельная таблица ``tasks`` (не code-``chunks``): у задач нет path/symbol/lines и
base/overlay-freshness. Зеркалит паттерн :class:`ChunkStore` — ленивый пул,
``register_vector`` на каждое соединение.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass

from pgvector.psycopg import Vector, register_vector
from psycopg_pool import ConnectionPool

_BM25_STRIP = re.compile(r"[^\w\s]")


def _bm25_query(text: str) -> str:
    cleaned = _BM25_STRIP.sub(" ", text).strip()
    return cleaned or "____nomatch____"


def build_task_text(title: str | None, description: str | None, criteria: list[str] | None) -> str:
    """Текст задачи для эмбеддинга и BM25: заголовок + описание + критерии."""
    parts = [title or "", description or ""]
    if criteria:
        parts.append("\n".join(c for c in criteria if c))
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


@dataclass
class TaskHit:
    key: str
    title: str
    status: str | None
    score: float


class TaskStore:
    """Хранилище задач в Postgres (таблица ``tasks``). Ленивый пул, register_vector."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 4) -> None:
        self.dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: ConnectionPool | None = None
        self._init_lock = threading.Lock()

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

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def existing_hash(self, key: str) -> str | None:
        """content_hash уже проиндексированной задачи (None если её нет)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content_hash FROM tasks WHERE key = %s", (key,)
            ).fetchone()
        return row[0] if row else None

    def upsert_task(self, row: TaskRow) -> None:
        sql = """
        INSERT INTO tasks (key, aliases, title, description, status, url,
                           content_hash, text, embedding)
        VALUES (%(key)s,%(aliases)s,%(title)s,%(description)s,%(status)s,%(url)s,
                %(content_hash)s,%(text)s,%(embedding)s)
        ON CONFLICT (key) DO UPDATE SET
            aliases=EXCLUDED.aliases, title=EXCLUDED.title,
            description=EXCLUDED.description, status=EXCLUDED.status,
            url=EXCLUDED.url, content_hash=EXCLUDED.content_hash,
            text=EXCLUDED.text, embedding=EXCLUDED.embedding
        """
        params = {
            "key": row.key, "aliases": row.aliases, "title": row.title,
            "description": row.description, "status": row.status, "url": row.url,
            "content_hash": row.content_hash, "text": row.text,
            "embedding": row.embedding,
        }
        with self._connect() as conn:
            conn.execute(sql, params)
            conn.commit()

    def update_meta(self, key: str, title: str, status: str | None,
                    url: str | None, aliases: list[str]) -> None:
        """Обновить лёгкие метаданные без переэмбеда (когда content_hash совпал)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET title=%s, status=%s, url=%s, aliases=%s WHERE key=%s",
                (title, status, url, aliases, key),
            )
            conn.commit()

    def search(self, query_text: str, query_embedding: list[float],
               top_k: int = 5, candidates: int = 50) -> list[TaskHit]:
        """Гибрид RRF (BM25 ⊕ ANN) по корпусу задач — без ref-фильтра."""
        sql = """
        WITH bm25 AS (
            SELECT id, RANK() OVER (ORDER BY pdb.score(id) DESC) AS rank
            FROM tasks WHERE text @@@ %(q)s
            ORDER BY pdb.score(id) DESC LIMIT %(cand)s
        ),
        ann AS (
            SELECT id, RANK() OVER (ORDER BY embedding <=> %(vec)s) AS rank
            FROM tasks
            ORDER BY embedding <=> %(vec)s LIMIT %(cand)s
        ),
        rrf AS (
            SELECT id, 1.0/(60+rank) AS s FROM bm25
            UNION ALL SELECT id, 1.0/(60+rank) AS s FROM ann
        )
        SELECT t.key, t.title, t.status, SUM(r.s) AS score
        FROM rrf r JOIN tasks t USING (id)
        GROUP BY t.id, t.key, t.title, t.status
        ORDER BY score DESC LIMIT %(k)s
        """
        params = {"q": _bm25_query(query_text), "vec": Vector(query_embedding),
                  "cand": candidates, "k": top_k}
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [TaskHit(key=k, title=t, status=s, score=float(sc))
                for (k, t, s, sc) in rows]
