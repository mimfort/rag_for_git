from __future__ import annotations
import threading
from dataclasses import dataclass
from datetime import datetime
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
    ann_distance: float | None = None   # cosine-дистанция ANN (PRI-202); None — не в ANN top-cand
    bm25_hit: bool = False              # был ли чанк лексическим (BM25) совпадением


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

    def find_embeddings_by_hashes(self, repo: str, hashes: list[str]) -> dict[str, list[float]]:
        """Готовые векторы по content_hash из любого ref репо (cross-branch reuse).

        Эмбеддинг детерминирован по тексту чанка (content_hash = sha256 текста) при
        фиксированной модели — переиспользуем вектор вместо повторного вызова Voyage.
        """
        if not hashes:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ON (content_hash) content_hash, embedding "
                "FROM chunks WHERE repo=%s AND content_hash = ANY(%s) AND embedding IS NOT NULL "
                "ORDER BY content_hash",
                (repo, list(hashes)),
            ).fetchall()
        return {h: list(v) for h, v in rows}

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

    def get_repo_vcs(self, repo: str) -> tuple[str, str] | None:
        """Платформа VCS репо: (provider, base_url) или None.

        Отсутствие таблицы (старый индекс без init_schema) равнозначно
        отсутствию записи — резолв провайдера откатится на ENV-дефолт."""
        import psycopg.errors
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT provider, base_url FROM repo_vcs WHERE repo=%s", (repo,)
                ).fetchone()
        except psycopg.errors.UndefinedTable:
            return None
        return (row[0], row[1]) if row else None

    def set_repo_vcs(self, repo: str, provider: str, base_url: str = "") -> None:
        """Записать/обновить платформу VCS репо (UPSERT по repo)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO repo_vcs (repo, provider, base_url, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (repo) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    base_url = EXCLUDED.base_url,
                    updated_at = now()
                """,
                (repo, provider, base_url),
            )
            conn.commit()

    def get_index_meta_row(self, repo: str, ref: str) -> tuple[str, datetime] | None:
        """SHA и время последней индексации для ref, или None.

        Как get_index_meta, но возвращает ещё updated_at. Отсутствие таблицы
        (старый индекс без init_schema) равнозначно отсутствию записи."""
        import psycopg.errors
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT sha, updated_at FROM index_meta WHERE repo=%s AND ref=%s",
                    (repo, ref),
                ).fetchone()
        except psycopg.errors.UndefinedTable:
            return None
        return (row[0], row[1]) if row else None

    def count_chunks(self, repo: str, ref: str) -> int:
        """Число чанков в (repo, ref)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*) FROM chunks WHERE repo=%s AND ref=%s", (repo, ref)
            ).fetchone()
        return row[0] if row else 0

    def list_base_members(self, repo: str, branch: str
                          ) -> list[tuple[str, str, str, int, str]]:
        """Состав base-индекса ветки для кластеризации подсистем (PRI-159):
        (path, symbol_fqn, content_hash, start_line, skeleton_hash) для ref=base:<branch>.
        skeleton_hash (PRI-165) считается на лету из text символа — ключ свежести сводок
        по структуре (правка тела не ре-стейлит)."""
        from reviewer.index.refs import base_ref
        from reviewer.index.chunker import symbol_skeleton_hash
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT path, symbol_fqn, content_hash, start_line, text FROM chunks "
                "WHERE repo=%s AND ref=%s", (repo, base_ref(branch))).fetchall()
        return [(p, s, h, sl, symbol_skeleton_hash(t)) for p, s, h, sl, t in rows]

    def list_refs(self, repo: str) -> list[str]:
        """Отсортированный список distinct ref репо (для поиска overlay)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ref FROM chunks WHERE repo=%s ORDER BY ref", (repo,)
            ).fetchall()
        return [r[0] for r in rows]

    def migrate_legacy_base(self, primary: str) -> int:
        """Перенести legacy ref='base' → 'base:<primary>' в chunks и index_meta.

        Конфликт-устойчиво и идемпотентно: если ветка base:<primary> уже была
        проиндексирована (есть копия по уникальному ключу (repo, ref, path,
        symbol_fqn)), legacy-строка не перетирает её, а удаляется. Повторный вызов —
        no-op (legacy 'base' уже отсутствует). Без переэмбеддинга — векторы
        сохраняются. Выполнять один раз после апгрейда.
        Возвращает число фактически перенесённых chunks (без учёта удалённых дублей).
        """
        target = f"base:{primary}"
        with self._connect() as conn:
            # Шаг 1: перенести только те legacy-строки, для которых в target нет копии.
            cur = conn.execute(
                """
                UPDATE chunks SET ref=%s
                WHERE ref='base'
                  AND NOT EXISTS (
                      SELECT 1 FROM chunks c2
                      WHERE c2.repo=chunks.repo AND c2.ref=%s
                        AND c2.path=chunks.path AND c2.symbol_fqn=chunks.symbol_fqn
                  )
                """,
                (target, target),
            )
            n = cur.rowcount  # фактически перенесённые чанки
            # Шаг 2: остаток legacy ('base'), у которого target-копия уже была — удалить.
            conn.execute("DELETE FROM chunks WHERE ref='base'")
            # index_meta: если целевая запись уже есть — просто удаляем legacy-запись.
            conn.execute(
                """
                INSERT INTO index_meta (repo, ref, sha, updated_at)
                SELECT repo, %s, sha, now() FROM index_meta WHERE ref='base'
                ON CONFLICT (repo, ref) DO NOTHING
                """,
                (target,),
            )
            conn.execute("DELETE FROM index_meta WHERE ref='base'")
            conn.commit()
        return n

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
                      changed_paths, top_k=20, candidates=50, *, base_ref="base") -> list[Retrieved]:
        where = ("repo=%(repo)s AND "
                 "((ref=%(base)s AND NOT (path = ANY(%(changed)s))) OR ref=%(overlay)s)")
        sql = f"""
        WITH bm25 AS (
            SELECT id, RANK() OVER (ORDER BY pdb.score(id) DESC) AS rank
            FROM chunks
            WHERE text @@@ %(q)s AND {where}
            ORDER BY pdb.score(id) DESC LIMIT %(cand)s
        ),
        ann AS (
            SELECT id, (embedding <=> %(vec)s) AS dist,
                   RANK() OVER (ORDER BY embedding <=> %(vec)s) AS rank
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
               SUM(r.s) AS score,
               MIN(a.dist) AS ann_dist,
               bool_or(b.id IS NOT NULL) AS bm25_hit
        FROM rrf r JOIN chunks c USING (id)
        LEFT JOIN ann a ON a.id = c.id
        LEFT JOIN bm25 b ON b.id = c.id
        GROUP BY c.id, c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text
        ORDER BY score DESC LIMIT %(k)s
        """
        params = {"repo": repo, "q": _bm25_query(query_text), "vec": Vector(query_embedding),
                  "overlay": overlay_ref, "changed": changed_paths,
                  "cand": candidates, "k": top_k, "base": base_ref}
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Retrieved(node_id=f"{p}#{f}", path=p, symbol_fqn=f, kind=k,
                          start_line=sl, end_line=el, text=t, score=float(sc),
                          ann_distance=(float(ad) if ad is not None else None),
                          bm25_hit=bool(bh))
                for (p, f, k, sl, el, t, sc, ad, bh) in rows]

    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths, *, base_ref="base"):
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
          AND ((c.ref=%(base)s AND NOT (c.path = ANY(%(changed)s))) OR c.ref=%(overlay)s)
        """
        params = {"repo": repo, "paths": [p for p, _ in pairs], "fqns": [f for _, f in pairs],
                  "changed": changed_paths, "overlay": overlay_ref, "base": base_ref}
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Retrieved(node_id=f"{p}#{f}", path=p, symbol_fqn=f, kind=k,
                          start_line=sl, end_line=el, text=t, score=0.0)
                for (p, f, k, sl, el, t) in rows]

    def fetch_nodes_at(self, repo, node_ids, ref):
        """Текст чанков по КОНКРЕТНОМУ ref (без слияния base/overlay).

        В отличие от fetch_nodes (правило свежести base∪overlay), отдаёт ровно
        запрошенный ref — нужно для раздельного взятия base- и overlay-версии
        символа при сравнении сигнатур (blast-radius).
        """
        if not node_ids:
            return []
        pairs = [nid.split("#", 1) for nid in node_ids if "#" in nid]
        if not pairs:
            return []
        sql = """
        SELECT c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text
        FROM chunks c JOIN unnest(%(paths)s::text[], %(fqns)s::text[]) AS q(p,f)
          ON c.path=q.p AND c.symbol_fqn=q.f
        WHERE c.repo=%(repo)s AND c.ref=%(ref)s
        """
        params = {"repo": repo, "paths": [p for p, _ in pairs],
                  "fqns": [f for _, f in pairs], "ref": ref}
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Retrieved(node_id=f"{p}#{f}", path=p, symbol_fqn=f, kind=k,
                          start_line=sl, end_line=el, text=t, score=0.0)
                for (p, f, k, sl, el, t) in rows]
