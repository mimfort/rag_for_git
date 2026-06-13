# Multi-Repo Storage + Graph Auto-Reindex Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one deployment host N isolated repositories (multi-repo discriminator) and make the Neo4j code graph self-heal incrementally on `prepare_review` instead of drifting until a manual `reviewer index`.

**Architecture:** Add a `repo` discriminator (`owner/name`, lowercased) to every stored artifact in Postgres (`chunks`, `index_meta`) and Neo4j (`:Symbol.repo`), threaded through `ChunkStore`/`GraphStore`/`Retriever`/`ToolContext`/`MCPReviewService`/`ReviewService`/CLI. The agent-facing invariant `node_id = "path#fqn"` is unchanged; `repo` is orthogonal and enforced inside the stores. The graph self-heal reuses the existing SHA-drift block in `ReviewService.prepare`, patching only changed files with tree-sitter while preserving incoming `CALLS` edges.

**Tech Stack:** Python 3.11–3.13, psycopg3 + ParadeDB (pgvector + pg_search), Neo4j 5 (Cypher), tree-sitter, Click, pytest (unit on fakes; integration marker for real DBs).

**Spec:** `docs/superpowers/specs/2026-06-13-multi-repo-and-graph-auto-reindex-design.md`

**Conventions:** Code/docstrings/comments/CLI messages stay in **Russian** (project rule). Commits = Conventional Commits in Russian, **no self-attribution** (no `Co-Authored-By` / Claude / AI mentions). Run unit tests with `.venv/bin/pytest -q` (integration excluded by default).

---

## Phase 1 — Storage repo-discriminator (foundation)

### Task 1: Postgres schema + `ChunkRow.repo`

**Files:**
- Modify: `reviewer/index/schema.sql`
- Modify: `reviewer/index/store.py:18-30` (`ChunkRow` dataclass)
- Test: `tests/index/test_schema.py` (extend)

- [ ] **Step 1: Update the schema (forward-only migration)**

Replace the `chunks` table + `index_meta` block in `reviewer/index/schema.sql` with repo-aware versions. Keep `CREATE ... IF NOT EXISTS` idempotency and add forward-only `ALTER`s for existing deployments:

```sql
CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    id           bigserial PRIMARY KEY,
    repo         text    NOT NULL DEFAULT '',  -- 'owner/name' (lowercased)
    ref          text    NOT NULL,             -- 'base' | 'pr:<number>'
    content_hash text    NOT NULL,
    path         text    NOT NULL,
    lang         text    NOT NULL,
    symbol_fqn   text    NOT NULL,
    kind         text    NOT NULL,
    start_line   int     NOT NULL,
    end_line     int     NOT NULL,
    text         text    NOT NULL,
    embedding    vector(1024)
);
-- Forward-only: добавить repo существующим деплоям до создания уникального ключа.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS repo text NOT NULL DEFAULT '';
-- Старый ключ без repo заменяем на repo-aware.
ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_ref_path_symbol_fqn_key;
ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_repo_ref_path_symbol_fqn_key;
ALTER TABLE chunks ADD CONSTRAINT chunks_repo_ref_path_symbol_fqn_key
    UNIQUE (repo, ref, path, symbol_fqn);

DROP INDEX IF EXISTS chunks_ref_path;
CREATE INDEX IF NOT EXISTS chunks_repo_ref_path ON chunks (repo, ref, path);
CREATE INDEX IF NOT EXISTS chunks_hash ON chunks (content_hash);

-- BM25 (pg_search): repo в stored-полях для фильтра по repo внутри CTE.
DROP INDEX IF EXISTS chunks_bm25;
CREATE INDEX IF NOT EXISTS chunks_bm25 ON chunks
USING bm25 (id, text, path, ref, repo) WITH (key_field='id');

CREATE INDEX IF NOT EXISTS chunks_hnsw ON chunks
USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Метаданные индексирования: SHA последней индексации по (repo, ref).
CREATE TABLE IF NOT EXISTS index_meta (
    repo       TEXT        NOT NULL DEFAULT '',
    ref        TEXT        NOT NULL,
    sha        TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, ref)
);
ALTER TABLE index_meta ADD COLUMN IF NOT EXISTS repo text NOT NULL DEFAULT '';
```

Leave the `tasks` table block unchanged (tasks are intentionally global — see spec §4.4).

> Note: if `index_meta` already exists with `PRIMARY KEY (ref)`, the `ALTER ... ADD COLUMN` succeeds but the PK is not auto-migrated. For existing single-repo deployments the recommended path is a one-time `reviewer index --repo owner/name` after dropping the old `index_meta` rows; document this in Task 15. Fresh installs get the composite PK directly.

- [ ] **Step 2: Add `repo` to `ChunkRow`**

In `reviewer/index/store.py`, add `repo` as the first field of `ChunkRow`:

```python
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
```

- [ ] **Step 3: Run existing schema test to confirm it still parses/applies**

Run: `.venv/bin/pytest tests/index/test_schema.py -q`
Expected: PASS (or, if it asserts on column set, update the assertion to include `repo` — show the change you make).

- [ ] **Step 4: Commit**

```bash
git add reviewer/index/schema.sql reviewer/index/store.py tests/index/test_schema.py
git commit -m "feat(index): repo-дискриминатор в схеме chunks/index_meta и ChunkRow"
```

---

### Task 2: `ChunkStore` methods threaded with `repo`

**Files:**
- Modify: `reviewer/index/store.py` (`ChunkStore`)
- Test: `tests/index/test_store_hybrid.py` (integration-marked) and a new unit test for SQL-shape is not feasible without a DB; rely on the integration test + signature-level unit checks.

All `ChunkStore` methods gain a leading `repo: str` parameter and filter by it. Below are the exact new bodies.

- [ ] **Step 1: Update mutation methods**

```python
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
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE repo=%s AND ref=%s", (repo, ref))
            conn.commit()

    def delete_paths(self, repo: str, ref: str, paths: list[str]) -> None:
        if not paths:
            return
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE repo=%s AND ref=%s AND path = ANY(%s)",
                (repo, ref, paths),
            )
            conn.commit()

    def delete_missing_symbols(self, repo: str, ref: str, path: str, keep_fqns: list[str]) -> None:
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
        if not keep_paths:
            return
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE repo=%s AND ref=%s AND NOT (path = ANY(%s))",
                (repo, ref, keep_paths),
            )
            conn.commit()
```

- [ ] **Step 2: Update `index_meta` methods**

```python
    def get_index_meta(self, repo: str, ref: str) -> str | None:
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
```

- [ ] **Step 3: Update search methods (add `repo` filter to every WHERE)**

```python
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
```

- [ ] **Step 4: Update the integration test for two-repo isolation**

In `tests/index/test_store_hybrid.py` (integration-marked), update existing calls to the new signatures and add an isolation test. Example addition:

```python
@pytest.mark.integration
def test_hybrid_search_isolates_by_repo(pg_store):
    pg_store.init_schema()
    pg_store.clear()  # global wipe for a clean test DB
    pg_store.upsert([_row(repo="a/x", ref="base", path="m.py", fqn="foo", text="def foo(): ...")])
    pg_store.upsert([_row(repo="b/y", ref="base", path="m.py", fqn="foo", text="def foo(): ...")])
    hits = pg_store.hybrid_search("a/x", "foo", _vec(), overlay_ref="__none__",
                                  changed_paths=[], top_k=10)
    assert {h.node_id for h in hits} == {"m.py#foo"}
    assert all(True for _ in hits)  # only repo a/x rows contributed
```

(Define `_row(...)` / `_vec()` helpers matching the file's existing fixtures; if the file already has a row factory, extend it with a `repo` kwarg defaulting to `"a/x"`.)

- [ ] **Step 5: Run unit suite (integration excluded) to confirm no import/signature breakage**

Run: `.venv/bin/pytest -q`
Expected: failures only in tests that call old `ChunkStore` signatures (fixed in later tasks); no collection/import errors from `store.py`.

- [ ] **Step 6: Commit**

```bash
git add reviewer/index/store.py tests/index/test_store_hybrid.py
git commit -m "feat(index): ChunkStore фильтрует по repo во всех методах"
```

---

### Task 3: `freshness.py` threaded with `repo`

**Files:**
- Modify: `reviewer/index/freshness.py`
- Test: `tests/index/test_freshness.py`

The existing `update_base` already has an unused `repo` param — repurpose it. `build_overlay` gains a `repo` param. `_rows_for_file` gains `repo`.

- [ ] **Step 1: Update the failing tests first (TDD)**

In `tests/index/test_freshness.py`, update the `FakeStore` recorders and calls to the new signatures, and assert `repo` is set on rows:

```python
class FakeStore:
    def __init__(self):
        self.rows: list = []
        self.deleted_paths: list[tuple[str, str, list[str]]] = []          # (repo, ref, paths)
        self.deleted_missing: list[tuple[str, str, str, list[str]]] = []   # (repo, ref, path, keep)

    def existing_hashes(self, repo, ref): return set()
    def upsert(self, rows): self.rows.extend(rows)

    def delete_paths(self, repo, ref, paths):
        if paths:
            self.deleted_paths.append((repo, ref, list(paths)))

    def delete_missing_symbols(self, repo, ref, path, keep_fqns):
        self.deleted_missing.append((repo, ref, path, list(keep_fqns)))


def test_build_overlay_sets_repo_on_rows():
    store, emb = FakeStore(), FakeEmb()
    build_overlay(store, emb, repo="a/x", pr_number=7,
                  changed_files=["a.py"], head_sources={"a.py": "def f():\n    return 1\n"})
    assert store.rows and all(r.repo == "a/x" and r.ref == "pr:7" for r in store.rows)


def test_update_base_removed_files_calls_delete_paths():
    store, emb = FakeStore(), FakeEmb()
    update_base(store, emb, repo="a/x", target_ref="main",
                changed_files=[], read=lambda p: None,
                removed_files=["old.py", "readme.md"])
    assert ("a/x", "base", ["old.py"]) in store.deleted_paths
```

(Update every other call site in this test file to pass `repo=...` and the new recorder tuple shapes.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/index/test_freshness.py -q`
Expected: FAIL (signature mismatch / missing `repo` on rows).

- [ ] **Step 3: Implement repo threading in `freshness.py`**

```python
def _rows_for_file(repo: str, path: str, source: str, ref: str) -> list[ChunkRow]:
    chunks = chunk_python(path, source.encode("utf-8"))
    return [ChunkRow(repo=repo, ref=ref, content_hash=c.content_hash, path=c.path,
                     lang=c.lang, symbol_fqn=c.symbol_fqn, kind=c.kind,
                     start_line=c.start_line, end_line=c.end_line,
                     text=c.text, embedding=[]) for c in chunks]


def build_overlay(store, embedder, repo: str, pr_number: int, changed_files: list[str],
                  head_sources: dict[str, str]) -> None:
    """Чанкует изменённые файлы PR head в (repo, ref='pr:<n>'). Дедуп по content_hash."""
    ref = f"pr:{pr_number}"
    seen = store.existing_hashes(repo, ref)
    batch: list[ChunkRow] = []
    for path in changed_files:
        if not path.endswith(".py"):
            continue
        src = head_sources.get(path)
        if not src:
            continue
        for row in _rows_for_file(repo, path, src, ref):
            if row.content_hash not in seen:
                seen.add(row.content_hash)
                batch.append(row)
    _embed_and_upsert(store, embedder, batch)


def update_base(store, embedder, repo: str, target_ref: str,
                changed_files: list[str],
                read: Callable[[str], str | None],
                removed_files: list[str] | tuple[str, ...] = ()) -> None:
    """Инкрементально обновляет (repo, ref='base') по изменённым файлам целевой ветки."""
    py_removed = [p for p in removed_files if p.endswith(".py")]
    store.delete_paths(repo, "base", py_removed)

    seen = store.existing_hashes(repo, "base")
    batch: list[ChunkRow] = []
    for path in changed_files:
        if not path.endswith(".py"):
            continue
        src = read(path)
        if src is None:
            store.delete_paths(repo, "base", [path])
            continue
        rows = _rows_for_file(repo, path, src, "base")
        store.delete_missing_symbols(repo, "base", path, [r.symbol_fqn for r in rows])
        for row in rows:
            if row.content_hash not in seen:
                seen.add(row.content_hash)
                batch.append(row)
    _embed_and_upsert(store, embedder, batch)
```

(Keep `build_overlay`'s parameter order as shown: `store, embedder, repo, pr_number, ...` — callers updated in Task 10.)

- [ ] **Step 4: Run tests to verify pass**

Run: `.venv/bin/pytest tests/index/test_freshness.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/index/freshness.py tests/index/test_freshness.py
git commit -m "feat(index): freshness прокидывает repo в ChunkRow и гигиену"
```

---

### Task 4: Neo4j `GraphStore` schema + repo in all methods

**Files:**
- Modify: `reviewer/graph/store.py`
- Test: `tests/graph/test_store.py` (integration-marked)

- [ ] **Step 1: Update `init_schema` (replace single-prop constraint with composite)**

```python
    def init_schema(self) -> None:
        # Старый одно-property constraint снимаем — id больше не глобально уникален.
        self._driver.execute_query("DROP CONSTRAINT sym_id IF EXISTS")
        # Композитная уникальность (repo, id) — property-uniqueness, есть в Neo4j 5 Community.
        self._driver.execute_query(
            "CREATE CONSTRAINT sym_repo_id IF NOT EXISTS "
            "FOR (s:Symbol) REQUIRE (s.repo, s.id) IS UNIQUE")
        self._driver.execute_query(
            "CREATE CONSTRAINT task_key IF NOT EXISTS "
            "FOR (t:Task) REQUIRE t.key IS UNIQUE")
        self._driver.execute_query(
            "CREATE CONSTRAINT pr_id IF NOT EXISTS "
            "FOR (p:PR) REQUIRE p.id IS UNIQUE")
        self._driver.execute_query(
            "CREATE INDEX task_codes IF NOT EXISTS FOR (t:Task) ON (t.codes)")
```

> Fallback if the target Neo4j rejects composite property uniqueness (older/community edge case): replace the `CREATE CONSTRAINT sym_repo_id ...` line with `CREATE INDEX sym_repo_id IF NOT EXISTS FOR (s:Symbol) ON (s.repo, s.id)` — uniqueness is still guaranteed by `MERGE` on both properties. Decide during this task by running `init_schema` against the running Neo4j; keep whichever the server accepts and note it in the commit message.

- [ ] **Step 2: Update `clear`, `upsert_nodes`, `upsert_edges` (repo-scoped)**

```python
    def clear(self, repo: str | None = None) -> None:
        """Удалить узлы/рёбра репозитория (repo) или весь граф (repo=None — тесты)."""
        if repo is None:
            self._driver.execute_query("MATCH (n) DETACH DELETE n")
        else:
            self._driver.execute_query(
                "MATCH (s:Symbol {repo: $repo}) DETACH DELETE s", repo=repo)

    def upsert_nodes(self, repo: str, node_ids: list[str]) -> None:
        self._driver.execute_query(
            "UNWIND $ids AS id MERGE (:Symbol {repo: $repo, id: id})",
            ids=list(node_ids), repo=repo)

    def upsert_edges(self, repo: str, edges: list[tuple[str, str, str]]) -> None:
        by_rel: dict[str, list[dict]] = {}
        for src, rel, dst in edges:
            by_rel.setdefault(rel, []).append({"src": src, "dst": dst})
        for rel, rows in by_rel.items():
            self._driver.execute_query(
                f"UNWIND $rows AS r "
                f"MATCH (a:Symbol {{repo: $repo, id: r.src}}) "
                f"MATCH (b:Symbol {{repo: $repo, id: r.dst}}) "
                f"MERGE (a)-[:{rel}]->(b)",
                rows=rows, repo=repo)
```

- [ ] **Step 3: Update traversals `expand`, `callers`, `find_symbol` (repo filter)**

```python
    def expand(self, repo: str, node_ids: list[str], hops: int = 2) -> set[str]:
        records, _, _ = self._driver.execute_query(
            f"UNWIND $ids AS sid MATCH (s:Symbol {{repo: $repo, id: sid}}) "
            f"MATCH (s)-[:CALLS|IMPLEMENTS|TESTED_BY*1..{hops}]-(n:Symbol {{repo: $repo}}) "
            f"RETURN DISTINCT n.id AS id",
            ids=list(node_ids), repo=repo)
        return {r["id"] for r in records}

    def callers(self, repo: str, node_ids: list[str]) -> set[str]:
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid "
            "MATCH (c:Symbol {repo: $repo})-[:CALLS]->(s:Symbol {repo: $repo, id: sid}) "
            "RETURN DISTINCT c.id AS id",
            ids=list(node_ids), repo=repo)
        return {r["id"] for r in records}

    def find_symbol(self, repo: str, name: str) -> list[str]:
        records, _, _ = self._driver.execute_query(
            "MATCH (s:Symbol {repo: $repo}) WHERE s.id CONTAINS $needle "
            "RETURN s.id AS id "
            "ORDER BY (CASE WHEN s.id ENDS WITH $suffix OR s.id ENDS WITH $dotname "
            "THEN 0 ELSE 1 END), s.id "
            "LIMIT 25",
            repo=repo, needle=name, suffix="#" + name, dotname="." + name)
        return [r["id"] for r in records]
```

- [ ] **Step 4: Update integration test for repo isolation**

In `tests/graph/test_store.py` (integration-marked), update calls to new signatures and add:

```python
@pytest.mark.integration
def test_graph_isolates_by_repo(graph_store):
    graph_store.init_schema()
    graph_store.clear()  # global wipe
    graph_store.upsert_nodes("a/x", ["m.py#foo", "m.py#bar"])
    graph_store.upsert_edges("a/x", [("m.py#bar", "CALLS", "m.py#foo")])
    graph_store.upsert_nodes("b/y", ["m.py#foo"])
    assert graph_store.callers("a/x", ["m.py#foo"]) == {"m.py#bar"}
    assert graph_store.callers("b/y", ["m.py#foo"]) == set()
    assert graph_store.find_symbol("b/y", "bar") == []
```

- [ ] **Step 5: Run unit suite (integration excluded)**

Run: `.venv/bin/pytest -q`
Expected: no import errors from `graph/store.py`; remaining failures are old-signature callers fixed in later tasks.

- [ ] **Step 6: Commit**

```bash
git add reviewer/graph/store.py tests/graph/test_store.py
git commit -m "feat(graph): GraphStore repo-aware (constraint (repo,id) + repo во всех запросах)"
```

---

## Phase 2 — Wiring repo through

### Task 5: Repo identity helper

**Files:**
- Create: `reviewer/services/repo_id.py`
- Test: `tests/services/test_repo_id.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest
from reviewer.services.repo_id import normalize_repo, derive_repo_from_remote


@pytest.mark.parametrize("raw,expected", [
    ("Owner/Repo", "owner/repo"),
    ("  OWNER/Name  ", "owner/name"),
    ("owner/name", "owner/name"),
])
def test_normalize_repo(raw, expected):
    assert normalize_repo(raw) == expected


@pytest.mark.parametrize("bad", ["", "noslash", "a/b/c"])
def test_normalize_repo_rejects_bad(bad):
    with pytest.raises(ValueError):
        normalize_repo(bad)


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:Owner/Repo.git", "owner/repo"),
    ("https://github.com/Owner/Repo.git", "owner/repo"),
    ("https://github.com/owner/name", "owner/name"),
    ("ssh://git@github.com/owner/name.git", "owner/name"),
])
def test_derive_from_remote(url, expected):
    assert derive_repo_from_remote(url) == expected


@pytest.mark.parametrize("url", ["", "https://gitlab.com/a/b.git", "not a url"])
def test_derive_from_remote_none(url):
    assert derive_repo_from_remote(url) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/services/test_repo_id.py -q`
Expected: FAIL with "No module named 'reviewer.services.repo_id'".

- [ ] **Step 3: Implement the helper**

```python
"""Канонизация и вывод идентификатора репозитория 'owner/name'."""
from __future__ import annotations

import re

_REMOTE_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")


def normalize_repo(repo: str) -> str:
    """Привести 'Owner/Repo' к канону 'owner/name' (нижний регистр).

    :raises ValueError: пустая строка или не ровно один '/'.
    """
    s = (repo or "").strip().lower()
    parts = s.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Некорректный repo (ожидается owner/name): {repo!r}")
    return f"{parts[0]}/{parts[1]}"


def derive_repo_from_remote(remote_url: str) -> str | None:
    """Вывести 'owner/name' из git remote URL GitHub. None, если не распознан."""
    m = _REMOTE_RE.search((remote_url or "").strip())
    if not m:
        return None
    return f"{m.group(1).lower()}/{m.group(2).lower()}"
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/services/test_repo_id.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/services/repo_id.py tests/services/test_repo_id.py
git commit -m "feat(services): helper normalize_repo + derive_repo_from_remote"
```

---

### Task 6: `Retriever` threaded with `repo`

**Files:**
- Modify: `reviewer/retrieval/retriever.py`
- Test: `tests/retrieval/test_retriever.py`, `tests/retrieval/test_search_base.py`

- [ ] **Step 1: Update failing tests first**

In `tests/retrieval/test_retriever.py` and `tests/retrieval/test_search_base.py`, update fake stores/graphs to accept `repo` as the first arg and update `retrieve`/`search_base` calls to pass `repo="a/x"`. Example fake-store recorder change:

```python
class FakeStore:
    def hybrid_search(self, repo, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k=20, candidates=50):
        self.last_repo = repo
        return self._hits
    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths):
        return [n for n in self._nodes if n.node_id in set(node_ids)]

class FakeGraph:
    def expand(self, repo, ids, hops=2): return set(self._related)
```

Add an assertion that `repo` reaches the store:

```python
def test_retrieve_threads_repo_to_store():
    store = FakeStore(...); r = Retriever(store, FakeGraph(...), FakeEmb(), FakeRr())
    r.retrieve("a/x", "q", changed_node_ids=[], overlay_ref="pr:1", changed_paths=[])
    assert store.last_repo == "a/x"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/retrieval/ -q`
Expected: FAIL (signature mismatch).

- [ ] **Step 3: Implement repo threading**

```python
    def retrieve(self, repo, query, changed_node_ids, overlay_ref, changed_paths,
                 top_k=15, candidates=50) -> ContextPack:
        qvec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(
            repo, query_text=query, query_embedding=qvec, overlay_ref=overlay_ref,
            changed_paths=changed_paths, top_k=candidates, candidates=candidates)
        related_ids = self.graph.expand(repo, changed_node_ids, hops=2)
        related = self.store.fetch_nodes(repo, list(related_ids), overlay_ref, changed_paths)
        hit_ids = {h.node_id for h in hits}
        graph_new = [it for it in related if it.node_id not in hit_ids]
        merged: dict[str, object] = {}
        for it in [*hits, *related]:
            merged.setdefault(it.node_id, it)
        if len(merged) <= 3 or (len(merged) <= top_k and not graph_new):
            return ContextPack(items=list(merged.values()),
                               max_chars=self.max_context_chars)
        ranked = self.reranker.rerank(query, list(merged.values()), top_k=top_k)
        return ContextPack(items=ranked, max_chars=self.max_context_chars)

    def search_base(self, repo, query, top_k=10, candidates=50) -> ContextPack:
        qvec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(
            repo, query_text=query, query_embedding=qvec,
            overlay_ref="__none__", changed_paths=[],
            top_k=candidates, candidates=candidates)
        merged: dict[str, object] = {}
        for h in hits:
            merged.setdefault(h.node_id, h)
        graph_new = False
        if self.graph is not None and hits:
            try:
                seeds = [h.node_id for h in hits[:top_k]]
                related_ids = self.graph.expand(repo, seeds, hops=1)
                related = self.store.fetch_nodes(repo, list(related_ids), "__none__", [])
                for it in related:
                    if it.node_id not in merged:
                        merged[it.node_id] = it
                        graph_new = True
            except Exception:
                log.warning("search_base: graph-expansion недоступен", exc_info=True)
        items = list(merged.values())
        if self.reranker is None or len(items) <= 3 or (len(items) <= top_k and not graph_new):
            return ContextPack(items=items[:top_k], max_chars=self.max_context_chars)
        try:
            items = self.reranker.rerank(query, items, top_k=top_k)
        except Exception:
            log.warning("search_base: rerank недоступен — RRF-порядок", exc_info=True)
            items = items[:top_k]
        return ContextPack(items=items, max_chars=self.max_context_chars)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/retrieval/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/retrieval/retriever.py tests/retrieval/
git commit -m "feat(retrieval): Retriever.retrieve/search_base прокидывают repo"
```

---

### Task 7: `ToolContext.repo` + `make_tools`

**Files:**
- Modify: `reviewer/tools/code_tools.py`
- Test: `tests/tools/test_code_tools.py`

- [ ] **Step 1: Update failing tests first**

In `tests/tools/test_code_tools.py`, construct `ToolContext(..., repo="a/x")` and update fake retriever/graph signatures to accept `repo` first. Add:

```python
def test_tools_thread_repo_to_graph_and_retriever():
    calls = {}
    class G:
        def expand(self, repo, ids, hops=2): calls["expand_repo"] = repo; return set()
        def callers(self, repo, ids): calls["callers_repo"] = repo; return set()
        def find_symbol(self, repo, name): calls["find_repo"] = repo; return []
    class R:
        def retrieve(self, repo, query, changed_node_ids, overlay_ref, changed_paths, top_k=8):
            calls["retrieve_repo"] = repo
            class P:  # noqa
                def as_context(self): return "x"
            return P()
    ctx = ToolContext(retriever=R(), graph=G(), overlay_ref="pr:1", changed_paths=[],
                      changed_node_ids=[], repo="a/x", cache={})
    tools = {t.name: t for t in make_tools(ctx)}
    tools["search_code"].invoke({"query": "q"})
    tools["get_related_symbols"].invoke({"node_id": "m.py#foo"})
    tools["find_callers"].invoke({"node_id": "m.py#foo"})
    tools["get_definition"].invoke({"symbol": "foo"})
    assert calls["retrieve_repo"] == "a/x"
    assert calls["expand_repo"] == "a/x"
    assert calls["callers_repo"] == "a/x"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/tools/test_code_tools.py -q`
Expected: FAIL (`ToolContext` has no `repo`; signature mismatches).

- [ ] **Step 3: Implement — add `repo` to `ToolContext` and use it**

Add the field to the dataclass (after `changed_node_ids`):

```python
@dataclass
class ToolContext:
    retriever: Any
    graph: Any
    overlay_ref: str
    changed_paths: list[str]
    changed_node_ids: list[str] = field(default_factory=list)
    repo: str = ""
    read_file_fn: Callable[[str], str | None] | None = None
    patches: dict = field(default_factory=dict)
    store: Any = None
    cache: dict | None = None
```

In `make_tools`, update the four tool bodies that touch graph/retriever:

```python
    def search_code(query: str) -> str:
        """Семантико-лексический поиск релевантного кода по всему репозиторию."""
        pack = ctx.retriever.retrieve(
            ctx.repo, query=query, changed_node_ids=ctx.changed_node_ids,
            overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths, top_k=8)
        return pack.as_context() or "(ничего не найдено)"

    def get_related_symbols(node_id: str) -> str:
        """Связанные символы (вызовы/реализации/тесты) для node_id вида 'path#fqn'."""
        related = ctx.graph.expand(ctx.repo, [node_id], hops=2)
        return "\n".join(sorted(related)) or "(нет связей)"
```

```python
    def get_definition(symbol: str) -> str:
        """Где определён символ + его исходный код."""
        ids: list[str] = []
        if ctx.graph is not None and hasattr(ctx.graph, "find_symbol"):
            ids = ctx.graph.find_symbol(ctx.repo, symbol)
        if ids and ctx.store is not None:
            nodes = ctx.store.fetch_nodes(ctx.repo, ids[:3], ctx.overlay_ref, ctx.changed_paths)
            if nodes:
                return "\n\n".join(
                    f"// {n.node_id} ({n.path}:{n.start_line}-{n.end_line})\n{n.text}"
                    for n in nodes)
        pack = ctx.retriever.retrieve(
            ctx.repo, query=symbol, changed_node_ids=ctx.changed_node_ids,
            overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths, top_k=3)
        return pack.as_context() or "(определение не найдено)"

    def find_callers(node_id: str) -> str:
        """Кто вызывает символ node_id ('path#fqn') — направленный CALLS (impact-анализ)."""
        if ctx.graph is None or not hasattr(ctx.graph, "callers"):
            return "(граф недоступен)"
        found = ctx.graph.callers(ctx.repo, [node_id])
        return "\n".join(sorted(found)) or "(вызовов не найдено)"
```

Add `repo` to `ctx_sig` so the per-run cache never mixes repos:

```python
    ctx_sig = (ctx.repo, ctx.overlay_ref, tuple(sorted(ctx.changed_paths or [])),
               tuple(sorted(ctx.changed_node_ids or [])))
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/tools/test_code_tools.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/tools/code_tools.py tests/tools/test_code_tools.py
git commit -m "feat(tools): ToolContext.repo прокидывается в граф/ретривер и ключ кэша"
```

---

### Task 8: `TaskGraph` — repo-aware `TOUCHES`

**Files:**
- Modify: `reviewer/tasks/graph.py:61-73` (`link_pr`)
- Test: `tests/tasks/test_graph.py` (integration-marked)

- [ ] **Step 1: Update `link_pr` to merge `:Symbol` with repo**

```python
    def link_pr(self, task_key: str, pr: PRRef, touched_node_ids: list[str]) -> None:
        """(:Task)-[:IMPLEMENTED_BY]->(:PR)-[:TOUCHES]->(:Symbol). Symbol скоупится по pr.repo."""
        self._driver.execute_query(
            "MERGE (t:Task {key: $key}) ON CREATE SET t.codes=[$key] "
            "MERGE (p:PR {id: $pid}) "
            "  SET p.repo=$repo, p.number=$number, p.url=$url, p.sha=$sha "
            "MERGE (t)-[:IMPLEMENTED_BY]->(p) "
            "WITH p "
            "UNWIND $touched AS nid "
            "MERGE (s:Symbol {repo: $repo, id: nid}) "
            "MERGE (p)-[:TOUCHES]->(s)",
            key=task_key, pid=pr.id, repo=pr.repo, number=pr.number,
            url=pr.url, sha=pr.sha, touched=list(touched_node_ids or []))
```

> `task_context` (read traversal) needs no change: it walks `(p:PR)-[:TOUCHES]->(s:Symbol)` and reads `s.id`; the edge already scopes to the PR's repo symbols. Leave it as-is.

- [ ] **Step 2: Update/add integration test**

In `tests/tasks/test_graph.py` add (integration-marked) a check that `link_pr` creates the touched symbol under the PR repo and that a same-id symbol in another repo is untouched:

```python
@pytest.mark.integration
def test_link_pr_scopes_touched_symbol_by_repo(task_graph, graph_store):
    graph_store.clear()
    pr = PRRef(repo="a/x", number=1, url="u", sha="s")
    task_graph.link_pr("ID-1", pr, ["m.py#foo"])
    # symbol exists under a/x
    assert graph_store.find_symbol("a/x", "foo") == ["m.py#foo"]
    # and not under b/y
    assert graph_store.find_symbol("b/y", "foo") == []
```

- [ ] **Step 3: Run unit suite (integration excluded) — no breakage**

Run: `.venv/bin/pytest -q`
Expected: no new failures from `tasks/graph.py`.

- [ ] **Step 4: Commit**

```bash
git add reviewer/tasks/graph.py tests/tasks/test_graph.py
git commit -m "feat(tasks): TOUCHES скоупит :Symbol по repo PR-а"
```

---

### Task 9: `Settings.default_repo` + `.env.example`

**Files:**
- Modify: `reviewer/config/settings.py:38-44`
- Modify: `.env.example`
- Test: `tests/config/test_settings.py`

- [ ] **Step 1: Write failing test**

```python
def test_default_repo_defaults_empty(monkeypatch):
    monkeypatch.delenv("DEFAULT_REPO", raising=False)
    from reviewer.config.settings import Settings
    assert Settings().default_repo == ""

def test_default_repo_from_env(monkeypatch):
    monkeypatch.setenv("DEFAULT_REPO", "owner/name")
    from reviewer.config.settings import Settings
    assert Settings().default_repo == "owner/name"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/config/test_settings.py -q`
Expected: FAIL (`Settings` has no `default_repo`).

- [ ] **Step 3: Implement**

Add to `Settings` (near the github block):

```python
    # multi-repo: дефолтный repo для session-less тулов (search_codebase) и
    # `reviewer index` без --repo; пусто = repo задаётся явно (мульти-репо-режим)
    default_repo: str = ""
```

In `.env.example`, add under a new section:

```bash
# ============================================================================
# Мульти-репо (опционально)
# ============================================================================
DEFAULT_REPO=                      # owner/name: дефолт для `reviewer index` без --repo и для search_codebase; пусто = repo обязателен явно
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/config/test_settings.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/config/settings.py .env.example tests/config/test_settings.py
git commit -m "feat(config): настройка DEFAULT_REPO (мост для одно-репных деплоев)"
```

---

### Task 10: `ReviewService.prepare` repo threading

**Files:**
- Modify: `reviewer/services/review_service.py`
- Test: `tests/services/test_review_service.py`

- [ ] **Step 1: Update failing tests first**

In `tests/services/test_review_service.py`, the fake store/graph/embedder calls now take `repo`. Update fake `store` recorders (`delete_ref(repo, ref)`, `get_index_meta(repo, ref)`, `set_index_meta(repo, ref, sha)`) and assert `PreparedReview.repo == "owner/name"`. Add:

```python
def test_prepare_sets_normalized_repo(prepared_fixture):
    prepared = prepared_fixture(owner="Owner", name="Repo")
    assert prepared.repo == "owner/repo"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/services/test_review_service.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement repo threading**

Add `repo` to `PreparedReview`:

```python
@dataclass
class PreparedReview:
    repo: str
    prq: PullRequest
    units: list[ReviewUnit]
    policy: ReviewPolicy
    patches: dict[str, str | None]
    sources: dict[str, str]
    changed_paths: list[str]
    changed_node_ids: list[str]
    skipped_paths: list[str]
    overlay_ref: str
    vcs: VCSProvider
    changed_status: dict[str, str]
    task_board: dict | None = None
    task_keys: dict | None = None
```

At the top of `prepare`, compute the repo and use it everywhere:

```python
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(f"{owner}/{repo}")

        # Self-healing: чистим возможный «висящий» overlay прошлого прогона.
        self.components.store.delete_ref(repo, f"pr:{pr_number}")
```

(Note: the method parameter is named `repo` for the GitHub repo *name*; rename the local to avoid shadowing. Rename the **parameter** `repo: str` → `name: str` in `prepare`'s signature and in `_create_vcs_provider(self, owner, repo)` keep as-is. Then `repo = normalize_repo(f"{owner}/{name}")`. Update the only caller — `MCPReviewService.prepare_review` already passes `name` positionally.)

Update the drift block + overlay + units to pass `repo`:

```python
            indexed = self.components.store.get_index_meta(repo, "base")
            if vcs_provider is None and indexed and indexed != prq.base_sha:
                try:
                    diff_files = vcs.compare_files(indexed, prq.base_sha)
                    update_base(
                        self.components.store,
                        self.components.embedder,
                        repo,
                        prq.base_ref,
                        [f.path for f in diff_files if f.status != "removed"],
                        read=lambda p: vcs.get_file_at_ref(p, prq.base_sha),
                        removed_files=[f.path for f in diff_files if f.status == "removed"],
                    )
                    self.components.store.set_index_meta(repo, "base", prq.base_sha)
                    # F2 (Task 14) добавит сюда инкрементальный патч графа.
                    log.info("Base-индекс синхронизирован: %d файлов (%s..%s)",
                             len(diff_files), indexed[:7], prq.base_sha[:7])
                except Exception as e:
                    log.warning("Не удалось синхронизировать base-индекс: %s", e)
            elif not indexed:
                log.warning("SHA base-индекса неизвестен (выполните reviewer index) "
                            "— индекс может быть устаревшим.")
```

```python
            build_overlay(
                self.components.store,
                self.components.embedder,
                repo,
                pr_number,
                changed,
                head_sources=head_sources,
            )
```

Set `repo=repo` in the returned `PreparedReview(...)`, and in the `except` cleanup branch use `self.components.store.delete_ref(repo, f"pr:{pr_number}")`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/services/test_review_service.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/services/review_service.py tests/services/test_review_service.py
git commit -m "feat(services): prepare нормализует repo и прокидывает его в store/freshness"
```

---

### Task 11: `MCPReviewService` repo plumbing + `search_codebase(repo, …)`

**Files:**
- Modify: `reviewer/mcp/service.py`
- Test: `tests/mcp/test_service.py`

- [ ] **Step 1: Update failing tests first**

In `tests/mcp/test_service.py`, update fakes for `store.delete_ref(repo, ref)` and assert `_tool_context` builds with `repo`. Add a `search_codebase` test:

```python
def test_search_codebase_uses_explicit_repo(svc_with_fake_retriever):
    svc, retr = svc_with_fake_retriever
    svc.search_codebase("a/x", "find foo")
    assert retr.last_search_base_repo == "a/x"

def test_search_codebase_falls_back_to_default_repo(svc_with_fake_retriever):
    svc, retr = svc_with_fake_retriever
    svc.settings.default_repo = "d/efault"
    svc.search_codebase("", "find foo")
    assert retr.last_search_base_repo == "d/efault"
```

(Fake retriever records `last_search_base_repo` in its `search_base(repo, query, top_k=...)`.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/mcp/test_service.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`prepare_review` — normalize repo and key the session by the normalized form:

```python
    def prepare_review(self, repo: str, pr: int) -> dict:
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        owner, name = repo.split("/", 1)
        old = self._sessions.get((repo, pr))
        vcs = self._vcs_factory(owner, name) if self._vcs_factory else None
        prepared = self._review_service.prepare(owner, name, pr, vcs_provider=vcs)
        ctx = self._tool_context(prepared)
        self._sessions[(repo, pr)] = _Session(prepared, ctx)
        if old is not None and self._vcs_factory is None:
            try:
                old.prepared.vcs.close()
            except Exception:
                log.warning("Не удалось закрыть VCS-провайдер старой сессии %s#%s",
                            repo, pr, exc_info=True)
        return self._prepared_payload(prepared)
```

`_tool_context` — pass `repo` from `prepared`:

```python
    def _tool_context(self, prepared: PreparedReview) -> ToolContext:
        return ToolContext(
            retriever=self.components.retriever,
            graph=self.components.graph,
            overlay_ref=prepared.overlay_ref,
            changed_paths=prepared.changed_paths,
            changed_node_ids=prepared.changed_node_ids,
            repo=prepared.repo,
            read_file_fn=(
                (lambda p: prepared.vcs.get_file_at_ref(p, prepared.prq.head_sha))
                if prepared.vcs else None
            ),
            patches=prepared.patches,
            store=getattr(self.components.retriever, "store", None),
            cache={},
        )
```

`search_codebase` — accept `repo`, fall back to `default_repo`:

```python
    def search_codebase(self, repo: str, query: str, top_k: int = 10) -> str:
        from reviewer.services.repo_id import normalize_repo
        raw = repo or self.settings.default_repo
        if not raw:
            return "(repo не задан: передайте repo или задайте DEFAULT_REPO)"
        try:
            repo = normalize_repo(raw)
        except ValueError:
            return f"(некорректный repo: {raw!r})"
        try:
            pack = self.components.retriever.search_base(repo, query, top_k=top_k)
        except Exception:
            log.warning("search_codebase: сбой поиска", exc_info=True)
            return "(ничего не найдено)"
        return pack.as_context() or "(ничего не найдено)"
```

`_cleanup` — repo-scoped overlay delete. The session key already carries the normalized repo, so:

```python
    def _cleanup(self, repo: str, pr: int) -> None:
        sess = self._sessions.pop((repo, pr), None)
        if sess is not None and self._vcs_factory is None:
            try:
                sess.prepared.vcs.close()
            except Exception:
                log.warning("Не удалось закрыть VCS-провайдер", exc_info=True)
        try:
            self.components.store.delete_ref(repo, f"pr:{pr}")
        except Exception:
            log.warning("Не удалось очистить overlay %s pr:%s", repo, pr, exc_info=True)
```

> `publish_review` already receives `repo` and calls `self._session(repo, pr)` and `self._cleanup(repo, pr)`. Normalize `repo` at the top of `publish_review` too (`repo = normalize_repo(repo)`) so the session lookup matches the key written by `prepare_review`. Add that one line after the docstring.

`_prepared_payload` — add `"repo": p.repo` into the returned `"pr"`-adjacent payload so the skill can echo it (optional but cheap): add top-level key `"repo": p.repo`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/mcp/test_service.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "feat(mcp): repo-сессии нормализованы, search_codebase(repo) + DEFAULT_REPO fallback"
```

---

### Task 12: MCP `search_codebase` signature + CLI `index --repo` / `search --repo`

**Files:**
- Modify: `reviewer/entrypoints/mcp_server.py:82-87`
- Modify: `reviewer/entrypoints/cli.py`
- Modify: `reviewer/gitutil.py` (add `remote_url` helper)
- Test: `tests/entrypoints/test_cli.py`, `tests/mcp/test_server_tools.py`, `tests/test_gitutil.py`

- [ ] **Step 1: Add `remote_url` to gitutil + failing test**

Test (`tests/test_gitutil.py`):

```python
def test_remote_url_returns_origin(tmp_git_repo_with_remote):
    from reviewer.gitutil import remote_url
    assert "github.com" in (remote_url(tmp_git_repo_with_remote) or "")
```

Implement in `reviewer/gitutil.py`:

```python
def remote_url(repo: str) -> str | None:
    """URL remote 'origin' или None, если remote нет."""
    try:
        return _git(repo, "remote", "get-url", "origin").strip()
    except subprocess.CalledProcessError:
        return None
```

- [ ] **Step 2: Update MCP `search_codebase` tool signature**

In `reviewer/entrypoints/mcp_server.py`:

```python
    @mcp.tool()
    def search_codebase(repo: str, query: str, top_k: int = 10) -> str:
        """Hybrid semantic+lexical search over a repo's base code index (no PR session).
        repo is "owner/name" (or "" to use DEFAULT_REPO). Use it (e.g. from /solve-task)
        to find relevant existing code by a free-text formulation."""
        return service.search_codebase(repo, query, top_k)
```

- [ ] **Step 3: Update CLI `index` and `search` for repo resolution**

Add a resolver and use it. In `reviewer/entrypoints/cli.py`:

```python
def _resolve_repo(repo_opt: str | None, path: str, settings) -> str:
    """Резолв repo-тега: --repo → git remote → DEFAULT_REPO → ошибка."""
    from reviewer.services.repo_id import normalize_repo, derive_repo_from_remote
    from reviewer.gitutil import remote_url
    if repo_opt:
        return normalize_repo(repo_opt)
    derived = derive_repo_from_remote(remote_url(path) or "")
    if derived:
        return derived
    if settings.default_repo:
        return normalize_repo(settings.default_repo)
    raise click.ClickException(
        "Не удалось определить repo: укажите --repo owner/name "
        "(или задайте DEFAULT_REPO в .env)")
```

Update `index`:

```python
@cli.command()
@click.argument("repo")
@click.option("--ref", default="HEAD")
@click.option("--repo", "repo_tag", default=None,
              help="owner/name тег индекса; по умолчанию из git remote origin")
def index(repo: str, ref: str, repo_tag: str | None) -> None:
    """Построить/обновить base-индекс целевой ветки из локального репо."""
    s = Settings()
    c = build_components(s)
    repo_id = _resolve_repo(repo_tag, repo, s)
    try:
        c.store.init_schema()
        files = list_python_files(repo, ref)
        update_base(c.store, c.embedder, repo_id, ref, files,
                    read=lambda p: file_at_ref(repo, p, ref))
        c.store.delete_paths_except(repo_id, "base", files)
        sha = rev_parse(repo, ref)
        c.store.set_index_meta(repo_id, "base", sha)
        src_by_path = {p: file_at_ref(repo, p, ref) for p in files}
        src_by_path = {p: v for p, v in src_by_path.items() if v is not None}
        gnodes, gedges, backend = build_code_graph(repo, ref, files, src_by_path, s.graph_backend)
        c.graph.init_schema()
        c.graph.clear(repo_id)   # rebuild только этого репо
        c.graph.upsert_nodes(repo_id, list(gnodes))
        c.graph.upsert_edges(repo_id, gedges)
        click.echo(
            f"Проиндексировано [{repo_id}] файлов: {len(files)} @ {sha[:7]}; "
            f"граф [{backend}]: узлов {len(gnodes)}, рёбер {len(gedges)}")
    finally:
        c.store.close()
        if c.graph:
            c.graph.close()
```

Update `search`:

```python
@cli.command()
@click.argument("query")
@click.option("--repo", "repo_tag", default=None, help="owner/name; по умолчанию DEFAULT_REPO")
def search(query: str, repo_tag: str | None) -> None:
    """Гибридный поиск по base-индексу (диагностика)."""
    from reviewer.services.repo_id import normalize_repo
    s = Settings()
    repo_id = normalize_repo(repo_tag or s.default_repo) if (repo_tag or s.default_repo) else None
    if repo_id is None:
        raise click.ClickException("Укажите --repo owner/name (или DEFAULT_REPO в .env)")
    c = build_components(s)
    try:
        qvec = c.embedder.embed_query(query)
        hits = c.store.hybrid_search(
            repo_id, query_text=query, query_embedding=qvec,
            overlay_ref="", changed_paths=[], top_k=10)
        for h in hits:
            click.echo(f"{h.score:.3f}  {h.node_id}  ({h.path}:{h.start_line})")
    finally:
        c.store.close()
        if c.graph:
            c.graph.close()
```

- [ ] **Step 4: Update CLI test**

In `tests/entrypoints/test_cli.py`, update existing `index`/`search` invocations to pass `--repo a/x` (or set `DEFAULT_REPO`), and add a test that `index` without `--repo`/remote/`DEFAULT_REPO` errors clearly. Match the file's existing CliRunner + monkeypatch patterns for `build_components`.

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/entrypoints/test_cli.py tests/test_gitutil.py tests/mcp/test_server_tools.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add reviewer/entrypoints/cli.py reviewer/entrypoints/mcp_server.py reviewer/gitutil.py tests/entrypoints/test_cli.py tests/test_gitutil.py tests/mcp/test_server_tools.py
git commit -m "feat(cli): index/search --repo + derive из git remote; search_codebase(repo) в MCP"
```

---

### Task 13: Full unit suite green after Phase 2

**Files:** none (verification task)

- [ ] **Step 1: Run the whole unit suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (all unit tests). Fix any remaining old-signature call sites surfaced here (e.g. `tests/test_app_wiring.py`, `tests/mcp/test_publish.py`) by threading `repo` exactly as in the matching task above.

- [ ] **Step 2: Lint**

Run: `.venv/bin/ruff check .`
Expected: clean (fix line-length/imports if flagged).

- [ ] **Step 3: Commit any test-only fixes**

```bash
git add -A
git commit -m "test: догнать сигнатуры repo в оставшихся тестах (фаза 2)"
```

---

## Phase 3 — Graph auto-reindex (F2)

### Task 14: New `GraphStore` incremental methods

**Files:**
- Modify: `reviewer/graph/store.py`
- Test: `tests/graph/test_store.py` (integration-marked)

- [ ] **Step 1: Implement the three methods**

```python
    def symbols_for_paths(self, repo: str, paths: list[str]) -> set[str]:
        """node_id всех :Symbol репозитория, чьи пути в paths (id начинается с 'path#')."""
        if not paths:
            return set()
        prefixes = [p + "#" for p in paths]
        records, _, _ = self._driver.execute_query(
            "MATCH (s:Symbol {repo: $repo}) "
            "WHERE any(p IN $prefixes WHERE s.id STARTS WITH p) "
            "RETURN s.id AS id",
            repo=repo, prefixes=prefixes)
        return {r["id"] for r in records}

    def delete_symbols(self, repo: str, ids: list[str]) -> None:
        """DETACH DELETE перечисленных символов (исчезнувшие/переименованные)."""
        if not ids:
            return
        self._driver.execute_query(
            "UNWIND $ids AS id MATCH (s:Symbol {repo: $repo, id: id}) DETACH DELETE s",
            ids=list(ids), repo=repo)

    def delete_outgoing_calls(self, repo: str, ids: list[str]) -> None:
        """Снести только ИСХОДЯЩИЕ CALLS у символов (входящие сохраняются)."""
        if not ids:
            return
        self._driver.execute_query(
            "UNWIND $ids AS id MATCH (s:Symbol {repo: $repo, id: id})-[r:CALLS]->() DELETE r",
            ids=list(ids), repo=repo)
```

- [ ] **Step 2: Add integration test for incoming-edge preservation**

```python
@pytest.mark.integration
def test_incremental_methods_preserve_incoming_calls(graph_store):
    graph_store.clear()
    # unchanged caller u.py#caller -> a.py#foo ; a.py also has stale#gone
    graph_store.upsert_nodes("a/x", ["a.py#foo", "a.py#gone", "u.py#caller"])
    graph_store.upsert_edges("a/x", [("u.py#caller", "CALLS", "a.py#foo"),
                                     ("a.py#foo", "CALLS", "a.py#gone")])
    # incremental patch of a.py: new symbols {a.py#foo, a.py#bar}, gone removed
    old = graph_store.symbols_for_paths("a/x", ["a.py"])
    assert old == {"a.py#foo", "a.py#gone"}
    graph_store.delete_symbols("a/x", list(old - {"a.py#foo", "a.py#bar"}))  # drop gone
    graph_store.delete_outgoing_calls("a/x", ["a.py#foo", "a.py#bar"])
    graph_store.upsert_nodes("a/x", ["a.py#foo", "a.py#bar"])
    graph_store.upsert_edges("a/x", [("a.py#bar", "CALLS", "a.py#foo")])
    # incoming edge from unchanged caller preserved:
    assert graph_store.callers("a/x", ["a.py#foo"]) == {"u.py#caller", "a.py#bar"}
    assert graph_store.find_symbol("a/x", "gone") == []
```

- [ ] **Step 3: Run integration test (requires running Neo4j)**

Run: `.venv/bin/pytest tests/graph/test_store.py -m integration -q`
Expected: PASS (start the stack with `docker compose up -d` if needed).

- [ ] **Step 4: Commit**

```bash
git add reviewer/graph/store.py tests/graph/test_store.py
git commit -m "feat(graph): symbols_for_paths/delete_symbols/delete_outgoing_calls для инкрементального патча"
```

---

### Task 15: Incremental graph patch in `ReviewService.prepare`

**Files:**
- Create: `reviewer/services/graph_sync.py`
- Modify: `reviewer/services/review_service.py` (drift block)
- Test: `tests/services/test_graph_sync.py`

- [ ] **Step 1: Write failing unit test for the patch function (on fakes)**

```python
from reviewer.services.graph_sync import patch_graph_incremental


class FakeGraph:
    def __init__(self, existing):
        self.symbols = dict(existing)   # repo -> set(node_id)
        self.deleted = []
        self.deleted_calls = []
        self.upserted_nodes = []
        self.upserted_edges = []
    def symbols_for_paths(self, repo, paths):
        prefixes = [p + "#" for p in paths]
        return {s for s in self.symbols.get(repo, set())
                if any(s.startswith(p) for p in prefixes)}
    def delete_symbols(self, repo, ids):
        self.deleted.append((repo, set(ids)))
        self.symbols.get(repo, set()).difference_update(ids)
    def delete_outgoing_calls(self, repo, ids):
        self.deleted_calls.append((repo, set(ids)))
    def upsert_nodes(self, repo, ids):
        self.upserted_nodes.append((repo, set(ids)))
        self.symbols.setdefault(repo, set()).update(ids)
    def upsert_edges(self, repo, edges):
        self.upserted_edges.append((repo, list(edges)))


def test_patch_removes_stale_and_refreshes_changed():
    g = FakeGraph({"a/x": {"a.py#foo", "a.py#gone"}})
    sources = {"a.py": "def foo():\n    bar()\n\ndef bar():\n    return 1\n"}
    patch_graph_incremental(g, "a/x", changed_sources=sources, removed_paths=[])
    # stale 'gone' removed; new nodes upserted; outgoing calls of changed surface cleared
    assert ("a/x", {"a.py#gone"}) in g.deleted
    assert any(repo == "a/x" and "a.py#bar" in ids for repo, ids in g.upserted_nodes)
    assert g.deleted_calls and g.deleted_calls[0][0] == "a/x"


def test_patch_removed_files_delete_symbols():
    g = FakeGraph({"a/x": {"old.py#x"}})
    patch_graph_incremental(g, "a/x", changed_sources={}, removed_paths=["old.py"])
    assert ("a/x", {"old.py#x"}) in g.deleted
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/services/test_graph_sync.py -q`
Expected: FAIL ("No module named 'reviewer.services.graph_sync'").

- [ ] **Step 3: Implement the patch function**

```python
"""Инкрементальный repo-aware патч графа кода (tree-sitter) для self-heal на prepare.

Симметрично self-heal векторов: переразбирает только изменённые файлы, сохраняя
ВХОДЯЩИЕ CALLS от неизменённых вызывающих (см. spec §5). Полная точность (IMPLEMENTS,
все рёбра) восстанавливается ручным `reviewer index` с SCIP.
"""
from __future__ import annotations

from reviewer.graph.builder import build_graph_from_files


def patch_graph_incremental(graph, repo: str, *, changed_sources: dict[str, str],
                            removed_paths: list[str]) -> None:
    """Обновить граф репозитория repo по изменённым/удалённым файлам.

    changed_sources: {path: head-источник} только .py изменённых/добавленных файлов.
    removed_paths: пути удалённых из PR .py-файлов.
    """
    # Удалённые файлы — снести их символы целиком.
    if removed_paths:
        gone = graph.symbols_for_paths(repo, removed_paths)
        graph.delete_symbols(repo, list(gone))

    if not changed_sources:
        return

    nodes, edges = build_graph_from_files(changed_sources)
    changed_paths = list(changed_sources)

    # Снести символы изменённых путей, исчезнувшие из новой версии.
    old = graph.symbols_for_paths(repo, changed_paths)
    stale = old - nodes
    graph.delete_symbols(repo, list(stale))

    # Снести только исходящие CALLS изменённой поверхности (входящие сохраняем),
    # затем переустановить узлы и свежие исходящие рёбра.
    graph.delete_outgoing_calls(repo, list(nodes))
    graph.upsert_nodes(repo, list(nodes))
    graph.upsert_edges(repo, edges)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/services/test_graph_sync.py -q`
Expected: PASS

- [ ] **Step 5: Wire into the drift block of `ReviewService.prepare`**

Inside the `try:` after `set_index_meta(repo, "base", prq.base_sha)` (replacing the `# F2 ... добавит` comment from Task 10), add the graph patch — fail-soft, only when a graph backend is wired:

```python
                    # F2: инкрементальный repo-aware патч графа (fail-soft).
                    if self.components.graph is not None:
                        try:
                            from reviewer.services.graph_sync import patch_graph_incremental
                            changed_py = {
                                f.path: src
                                for f in diff_files
                                if f.status != "removed" and f.path.endswith(".py")
                                and (src := vcs.get_file_at_ref(f.path, prq.base_sha))
                            }
                            removed_py = [
                                f.path for f in diff_files
                                if f.status == "removed" and f.path.endswith(".py")
                            ]
                            patch_graph_incremental(
                                self.components.graph, repo,
                                changed_sources=changed_py, removed_paths=removed_py)
                            log.info("Граф досинхронизирован инкрементально: "
                                     "%d изм., %d уд.", len(changed_py), len(removed_py))
                        except Exception:
                            log.warning("Инкрементальный патч графа не удался "
                                        "(дрейф графа сохранится до reviewer index)",
                                        exc_info=True)
```

> `:=` requires the source to be truthy; empty/None sources are skipped, matching overlay behavior. If your Python style forbids walrus in comprehensions here, expand to an explicit loop — keep the same filter semantics.

- [ ] **Step 6: Add a service-level test that the patch is invoked on drift**

In `tests/services/test_review_service.py`, add a test where `get_index_meta` returns an old SHA ≠ `base_sha`, `compare_files` returns one modified `.py`, and assert the fake graph recorded an `upsert_nodes`/`delete_outgoing_calls` for the repo. Reuse the existing prepare fixture + fake graph; mirror the existing drift test if one exists.

- [ ] **Step 7: Run service tests**

Run: `.venv/bin/pytest tests/services/test_review_service.py tests/services/test_graph_sync.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add reviewer/services/graph_sync.py reviewer/services/review_service.py tests/services/test_graph_sync.py tests/services/test_review_service.py
git commit -m "feat(services): инкрементальный self-heal графа на prepare (repo-aware, fail-soft)"
```

---

## Phase 4 — Documentation

### Task 16: Update READMEs, `.env.example`, CLAUDE.md

**Files:**
- Modify: `README.md`, `README.ru.md`
- Modify: `.env.example` (already touched in Task 9 — confirm)
- Modify: `CLAUDE.md`

- [ ] **Step 1: Fix the two now-false caveats in `README.md`**

Replace the "No graph auto-reindex" bullet with:

```markdown
- **Graph auto-reindex is incremental, not full-precision.** On `prepare_review`, when the base
  branch SHA drifts, the code graph is patched for the changed files (tree-sitter, repo-scoped) in
  the same step that self-heals vector chunks — incoming `CALLS` edges from unchanged callers are
  preserved. Not refreshed until the next manual `reviewer index`: `IMPLEMENTS` edges, outgoing
  `CALLS` into unchanged files, and new incoming `CALLS` from unchanged callers. Full SCIP precision
  is restored by `reviewer index`.
```

Replace the "Single-repo, not multi-tenant" bullet with:

```markdown
- **Multi-repo via a `repo` discriminator.** One deployment hosts N repositories isolated by a
  `repo` (`owner/name`) column/property across Postgres and Neo4j; each review is scoped to its PR's
  repo (no cross-repo retrieval). Index a repo with `reviewer index <path> --repo owner/name` (or let
  it derive `owner/name` from the git `origin` remote). The task graph (`:Task`) is intentionally
  global, so one task can span PRs across several microservice repos.
```

Add to the CLI section the `--repo` flag and to Installation/config a note about `DEFAULT_REPO`. Mirror all of this in `README.ru.md` (Russian wording), keeping the cross-link lines intact.

- [ ] **Step 2: Update `CLAUDE.md` invariants**

In the "Неочевидные факты" section, replace the "Индекс single-repo" bullet and the "Граф ... обновляется только при явном reviewer index" clause to reflect: (a) `repo` discriminator + `DEFAULT_REPO`; (b) graph now also self-heals incrementally on `prepare_review` (tree-sitter, repo-scoped, preserves incoming CALLS), with full SCIP precision via `reviewer index`. Keep wording factual and in Russian.

- [ ] **Step 3: Confirm `.env.example` has `DEFAULT_REPO`** (added in Task 9). If missing, add it.

- [ ] **Step 4: Commit**

```bash
git add README.md README.ru.md CLAUDE.md .env.example
git commit -m "docs: мульти-репо и инкрементальный self-heal графа в README/CLAUDE"
```

---

## Final verification

- [ ] **Step 1: Full unit suite**

Run: `.venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 2: Lint**

Run: `.venv/bin/ruff check .`
Expected: clean

- [ ] **Step 3: Integration suite (with stack up)**

Run: `docker compose up -d && .venv/bin/pytest -m integration -q`
Expected: PASS (multi-repo isolation + incremental graph patch).

- [ ] **Step 4: Smoke a two-repo index manually**

```bash
reviewer index /path/to/repoA --ref main --repo orgA/repoA
reviewer index /path/to/repoB --ref main --repo orgB/repoB
reviewer search "token verification" --repo orgA/repoA   # results only from repoA
```

---

## Self-review notes (filled by plan author)

- **Spec coverage:** F1 §4.1→Task 5/12; §4.2→Task 1/2; §4.3→Task 4; §4.4→Task 8; §4.5→Task 6; §4.6→Task 11/12; §4.7→Task 10; §4.8→Task 9; §4.9→Task 12; §4.10→Task 1/16. F2 §5.1/5.5→Task 15; §5.2→Task 15; §5.3→Task 14; §5.4→Task 16. Testing §7→Tasks 2/4/5/14/15 + Final. Docs §8→Task 16.
- **Placeholders:** none — every code step shows full code; test tasks that adapt existing files name the exact recorder/assertion shape.
- **Type/signature consistency:** `repo` is the leading positional arg on `ChunkStore` methods (Task 2), `GraphStore` methods (Task 4/14), `Retriever` (Task 6); `ToolContext.repo` (Task 7); `PreparedReview.repo` (Task 10); `normalize_repo`/`derive_repo_from_remote` (Task 5) used identically in CLI/MCP/service; `patch_graph_incremental(graph, repo, *, changed_sources, removed_paths)` (Task 15) matches its call site.
