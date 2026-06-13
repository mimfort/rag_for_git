from __future__ import annotations
import threading
from dataclasses import dataclass
from pathlib import Path
import re
from pgvector.psycopg import register_vector, Vector
from psycopg_pool import ConnectionPool

_BM25_STRIP = re.compile(r"[^\w\s]")

def _bm25_query(text: str) -> str:
    cleaned = _BM25_STRIP.sub(" ", text).strip()
    return cleaned or "____nomatch____"

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()


@dataclass
class ChunkRow:
    repo: str
    ref: str
    content_hash: str
    path: str
    lang: str
    symbol_fqn: str
    kind: str
    start_line: int
    end_line: int
    text: str
    embedding: list[float]


@dataclass
class Retrieved:
    node_id: str
    path: str
    symbol_fqn: str
    kind: str
    start_line: int
    end_line: int
    text: str
    score: float


class ChunkStore:
    """Хранилище чанков кода в Postgres с pgvector + bm25.

    Использует пул соединений :class:`psycopg_pool.ConnectionPool`;
    инициализация пула откладывается до первого запроса (lazy).
    Для каждого нового соединения автоматически вызывается
    ``register_vector(conn)``, без этого pgvector-типы в пуле не работают.
    """

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
                        self.dsn,
                        min_size=self._min_size,
                        max_size=self._max_size,
                        open=False,
                        configure=lambda conn: register_vector(conn),
                    )
                    self._pool.open()
        return self._pool

    def _connect(self):
        """Вернуть контекстный менеджер соединения из пула.

        Совместим с существующими вызовами ``with store._connect() as conn:``.
        """
        return self._ensure_pool().connection()

    def close(self) -> None:
        """Закрыть пул соединений, если он был создан."""
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def clear(self, repo: str | None = None) -> None:
        """Удалить чанки репозитория (repo) или весь индекс (repo=None — для тестов)."""
        with self._connect() as conn:
            if repo is None:
                conn.execute("TRUNCATE chunks RESTART IDENTITY")
            else:
                conn.execute("DELETE FROM chunks WHERE repo = %s", (repo,))
            conn.commit()

    def upsert(self, rows: list[ChunkRow]) -> None:
        sql = """
        INSERT INTO chunks (repo, ref, content_hash, path, lang, symbol_fqn, kind,
                            start_line, end_line, text, embedding)
        VALUES (%(repo)s,%(ref)s,%(content_hash)s,%(path)s,%(lang)s,%(symbol_fqn)s,%(kind)s,
                %(start_line)s,%(end_line)s,%(text)s,%(embedding)s)
        ON CONFLICT (repo, ref, path, symbol_fqn) DO UPDATE SET
            content_hash=EXCLUDED.content_hash, kind=EXCLUDED.kind,
            start_line=EXCLUDED.start_line, end_line=EXCLUDED.end_line,
            text=EXCLUDED.text, embedding=EXCLUDED.embedding
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, [r.__dict__ for r in rows])
            conn.commit()

    def existing_hashes(self, repo: str, ref: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content_hash FROM chunks WHERE repo=%s AND ref=%s", (repo, ref)
            ).fetchall()
        return {r[0] for r in rows}

    def delete_ref(self, repo: str, ref: str) -> None:
        """Удалить все чанки указанного ref (например, эфемерный overlay pr:N после ревью)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE repo=%s AND ref=%s", (repo, ref))
            conn.commit()

    def get_index_meta(self, repo: str, ref: str) -> str | None:
        """Вернуть SHA последней индексации для ref, или None если запись отсутствует.

        Отсутствие самой таблицы (индекс построен старой версией, init_schema не
        выполнялся) равнозначно отсутствию записи — review не должен падать."""
        import psycopg.errors
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT sha FROM index_meta WHERE repo=%s AND ref=%s", (repo, ref)
                ).fetchone()
        except psycopg.errors.UndefinedTable:
            return None
        return row[0] if row else None

    def set_index_meta(self, repo: str, ref: str, sha: str) -> None:
        """Записать/обновить SHA индексации для ref (UPSERT, обновляет updated_at)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO index_meta (repo, ref, sha, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (repo, ref) DO UPDATE SET sha = EXCLUDED.sha, updated_at = now()
                """,
                (repo, ref, sha),
            )
            conn.commit()

    def delete_paths(self, repo: str, ref: str, paths: list[str]) -> None:
        """Удалить чанки указанных путей для ref (гигиена: удалённые файлы из индекса).

        Пустой список — no-op.
        """
        if not paths:
            return
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE repo=%s AND ref=%s AND path = ANY(%s)",
                (repo, ref, paths),
            )
            conn.commit()

    def delete_missing_symbols(self, repo: str, ref: str, path: str, keep_fqns: list[str]) -> None:
        """Удалить из индекса символы path, отсутствующие в keep_fqns (гигиена: переименованные/удалённые символы).

        Пустой keep_fqns означает удалить все чанки указанного path.
        """
        with self._connect() as conn:
            if keep_fqns:
                conn.execute(
                    "DELETE FROM chunks WHERE repo=%s AND ref=%s AND path=%s "
                    "AND NOT (symbol_fqn = ANY(%s))",
                    (repo, ref, path, keep_fqns),
                )
            else:
                conn.execute(
                    "DELETE FROM chunks WHERE repo=%s AND ref=%s AND path=%s",
                    (repo, ref, path),
                )
            conn.commit()

    def delete_paths_except(self, repo: str, ref: str, keep_paths: list[str]) -> None:
        """Удалить из индекса все пути ref, кроме перечисленных в keep_paths (гигиена: файлы удалённые из репо).

        Пустой keep_paths — no-op (защита от случайного полного удаления индекса).
        """
        if not keep_paths:
            return
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE repo=%s AND ref=%s AND NOT (path = ANY(%s))",
                (repo, ref, keep_paths),
            )
            conn.commit()

    def hybrid_search(self, repo, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k=20, candidates=50) -> list[Retrieved]:
        where = ("repo=%(repo)s AND "
                 "((ref='base' AND NOT (path = ANY(%(changed)s))) OR ref=%(overlay)s)")
        sql = f"""
        WITH bm25 AS (
            SELECT id, RANK() OVER (ORDER BY pdb.score(id) DESC) AS rank
            FROM chunks
            WHERE text @@@ %(q)s AND {where}
            ORDER BY pdb.score(id) DESC LIMIT %(cand)s
        ),
        ann AS (
            SELECT id, RANK() OVER (ORDER BY embedding <=> %(vec)s) AS rank
            FROM chunks
            WHERE {where}
            ORDER BY embedding <=> %(vec)s LIMIT %(cand)s
        ),
        rrf AS (
            SELECT id, 1.0/(60+rank) AS s FROM bm25
            UNION ALL
            SELECT id, 1.0/(60+rank) AS s FROM ann
        )
        SELECT c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text,
               SUM(r.s) AS score
        FROM rrf r JOIN chunks c USING (id)
        GROUP BY c.id, c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text
        ORDER BY score DESC LIMIT %(k)s
        """
        params = {"repo": repo, "q": _bm25_query(query_text), "vec": Vector(query_embedding),
                  "overlay": overlay_ref, "changed": changed_paths,
                  "cand": candidates, "k": top_k}
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Retrieved(node_id=f"{p}#{f}", path=p, symbol_fqn=f, kind=k,
                          start_line=sl, end_line=el, text=t, score=float(sc))
                for (p, f, k, sl, el, t, sc) in rows]

    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths):
        if not node_ids:
            return []
        pairs = [nid.split("#", 1) for nid in node_ids if "#" in nid]
        if not pairs:
            return []
        sql = """
        SELECT c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text
        FROM chunks c JOIN unnest(%(paths)s::text[], %(fqns)s::text[]) AS q(p,f)
          ON c.path=q.p AND c.symbol_fqn=q.f
        WHERE c.repo=%(repo)s
          AND ((c.ref='base' AND NOT (c.path = ANY(%(changed)s))) OR c.ref=%(overlay)s)
        """
        params = {"repo": repo, "paths": [p for p, _ in pairs], "fqns": [f for _, f in pairs],
                  "changed": changed_paths, "overlay": overlay_ref}
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Retrieved(node_id=f"{p}#{f}", path=p, symbol_fqn=f, kind=k,
                          start_line=sl, end_line=el, text=t, score=0.0)
                for (p, f, k, sl, el, t) in rows]
