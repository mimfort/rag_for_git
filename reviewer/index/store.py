from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re
import psycopg
from pgvector.psycopg import register_vector, Vector

_BM25_STRIP = re.compile(r"[^\w\s]")

def _bm25_query(text: str) -> str:
    cleaned = _BM25_STRIP.sub(" ", text).strip()
    return cleaned or "____nomatch____"

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()


@dataclass
class ChunkRow:
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
    def __init__(self, dsn: str):
        self.dsn = dsn

    def init_schema(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def _connect(self):
        conn = psycopg.connect(self.dsn)
        register_vector(conn)
        return conn

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("TRUNCATE chunks RESTART IDENTITY")
            conn.commit()

    def upsert(self, rows: list[ChunkRow]) -> None:
        sql = """
        INSERT INTO chunks (ref, content_hash, path, lang, symbol_fqn, kind,
                            start_line, end_line, text, embedding)
        VALUES (%(ref)s,%(content_hash)s,%(path)s,%(lang)s,%(symbol_fqn)s,%(kind)s,
                %(start_line)s,%(end_line)s,%(text)s,%(embedding)s)
        ON CONFLICT (ref, path, symbol_fqn) DO UPDATE SET
            content_hash=EXCLUDED.content_hash, kind=EXCLUDED.kind,
            start_line=EXCLUDED.start_line, end_line=EXCLUDED.end_line,
            text=EXCLUDED.text, embedding=EXCLUDED.embedding
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, [r.__dict__ for r in rows])
            conn.commit()

    def existing_hashes(self, ref: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content_hash FROM chunks WHERE ref=%s", (ref,)
            ).fetchall()
        return {r[0] for r in rows}

    def delete_ref(self, ref: str) -> None:
        """Удалить все чанки указанного ref (например, эфемерный overlay pr:N после ревью)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE ref = %s", (ref,))
            conn.commit()

    def hybrid_search(self, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k=20, candidates=50) -> list[Retrieved]:
        where = "((ref='base' AND NOT (path = ANY(%(changed)s))) OR ref=%(overlay)s)"
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
        params = {"q": _bm25_query(query_text), "vec": Vector(query_embedding), "overlay": overlay_ref,
                  "changed": changed_paths, "cand": candidates, "k": top_k}
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Retrieved(node_id=f"{p}#{f}", path=p, symbol_fqn=f, kind=k,
                          start_line=sl, end_line=el, text=t, score=float(sc))
                for (p, f, k, sl, el, t, sc) in rows]

    def fetch_nodes(self, node_ids, overlay_ref, changed_paths):
        if not node_ids:
            return []
        pairs = [nid.split("#", 1) for nid in node_ids if "#" in nid]
        if not pairs:
            return []
        sql = """
        SELECT c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text
        FROM chunks c JOIN unnest(%(paths)s::text[], %(fqns)s::text[]) AS q(p,f)
          ON c.path=q.p AND c.symbol_fqn=q.f
        WHERE (c.ref='base' AND NOT (c.path = ANY(%(changed)s))) OR c.ref=%(overlay)s
        """
        params = {"paths": [p for p, _ in pairs], "fqns": [f for _, f in pairs],
                  "changed": changed_paths, "overlay": overlay_ref}
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Retrieved(node_id=f"{p}#{f}", path=p, symbol_fqn=f, kind=k,
                          start_line=sl, end_line=el, text=t, score=0.0)
                for (p, f, k, sl, el, t) in rows]
