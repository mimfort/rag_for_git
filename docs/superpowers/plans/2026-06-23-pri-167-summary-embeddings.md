# PRI-167 — Векторизация сводок подсистем + отбор по близости (top-k) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** На индексе эмбедить сводки подсистем (дедуп по `source_hash`) и при масштабе (число сводок > порога) отбирать их по близости к запросу (ANN top-k), сохраняя бэк-компат «отдать все» для малых репо / запросов без `query`.

**Architecture:** Эмбеддинг пишется в существующую колонку `subsystem_summaries.embedding` при `index_subsystem_summary` (Voyage `embed_documents`, дедуп по `source_hash` через `COALESCE`). Поисковый путь `get_subsystem_summaries(query, top_k)` при числе сводок выше порога делает чистый ANN-поиск (`embedding <=> qvec`), иначе отдаёт все. Серверный self-heal `backfill_summary_embeddings` дозаполняет легаси-сводки с `embedding IS NULL` из хранимого текста (без LLM). Порог — env-дефолт + per-repo `.review.yml` override, зеркало `summary_cluster_depth`.

**Tech Stack:** Python 3.11–3.13, psycopg + pgvector (HNSW cosine), Voyage (`voyage-code-3`, dim 1024), FastMCP, pytest. Спека: `docs/superpowers/specs/2026-06-23-pri-167-summary-embeddings-design.md`.

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения CLI. Английский — только тело `plugin/skills/**/SKILL.md` (токены), но инструкции отвечать пользователю по-русски.
- Линт: `.venv/bin/ruff check .` — line-length 100, target py311.
- Voyage free tier: **3 RPM / 10K TPM** — лишние эмбеддинги недопустимы; дедуп по `source_hash` обязателен; query-эмбеддинг LRU-кэширован (`reviewer/index/embeddings.py:49`).
- Коммиты: **Conventional Commits на русском**, **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- Размерность эмбеддинга — **1024** (`vector(1024)`, `Settings.embedding_dim`).
- Unit-тесты гоняются `.venv/bin/pytest -q` (по умолчанию исключают `integration`); integration — `.venv/bin/pytest -m integration` (нужны Postgres :5433 + Neo4j).
- Ветка работы: `feat/pri-167-summary-embeddings` (уже создана).

---

## Файловая структура

| Файл | Что меняем |
|---|---|
| `reviewer/config/settings.py` | поле `summary_topk_threshold: int = 20` |
| `reviewer/policy/policy.py` | поле `summary_topk_threshold` + `from_settings` + `load` |
| `reviewer/mcp/service.py` | `_resolve_summary_topk_threshold`; эмбеддинг+дедуп в `index_subsystem_summary`; query-путь в `get_subsystem_summaries`; новый `backfill_summary_embeddings` |
| `reviewer/index/summary_store.py` | `embedding` в `upsert_summary` (COALESCE); `search_summaries`; `count_summaries`; `get_pending_embeddings`; `set_embedding` |
| `reviewer/index/schema.sql` | HNSW-индекс на `subsystem_summaries.embedding` |
| `reviewer/entrypoints/mcp_server.py` | расширить тул `get_subsystem_summaries` (`query`, `top_k`); зарегистрировать `backfill_summary_embeddings` |
| `plugin/skills/ask/SKILL.md` | передавать `query` в `get_subsystem_summaries` |
| `plugin/skills/pr-walkthrough/SKILL.md` | передавать `query` |
| `plugin/skills/summarize-subsystems/SKILL.md` | вызывать `backfill_summary_embeddings` после LLM-прохода |
| `tests/mcp/test_subsystem_summaries.py` | unit: дедуп, query-путь, порог, backfill, резолвер |
| `tests/index/test_summary_store.py` (новый) | integration: ANN-поиск, count, backfill roundtrip |
| `tests/mcp/test_server.py` | guard: добавить `backfill_summary_embeddings` в набор тулов |
| `tests/skills/test_ask_uses_summaries.py` | guard: ask передаёт `query`; summarize вызывает backfill |

---

## Task 1: Конфиг порога — `summary_topk_threshold` (settings + policy + резолвер)

**Files:**
- Modify: `reviewer/config/settings.py:71-74`
- Modify: `reviewer/policy/policy.py:22,41,55,85`
- Modify: `reviewer/mcp/service.py:331-360` (добавить метод после `_resolve_summary_depth`)
- Test: `tests/mcp/test_subsystem_summaries.py` (резолвер) + `tests/policy/test_policy.py` (политика)

**Interfaces:**
- Produces: `Settings.summary_topk_threshold: int` (=20); `ReviewPolicy.summary_topk_threshold: int`; `MCPReviewService._resolve_summary_topk_threshold(repo: str, branch: str) -> tuple[int, str]` (значение, источник `"env"`/`".review.yml"`).

- [ ] **Step 1: Write the failing tests (резолвер + политика)**

В `tests/mcp/test_subsystem_summaries.py` добавь в конец файла:

```python
def test_resolve_summary_topk_threshold_override_from_review_yml():
    svc = _svc_with_vcs(_FakeVCS("summary_topk_threshold: 5"))
    assert svc._resolve_summary_topk_threshold("o/n", "dev") == (5, ".review.yml")


def test_resolve_summary_topk_threshold_no_key_falls_back_to_env():
    svc = _svc_with_vcs(_FakeVCS("severity_threshold: high"))
    val, source = svc._resolve_summary_topk_threshold("o/n", "dev")
    assert val == svc.settings.summary_topk_threshold
    assert source == "env"


def test_settings_default_summary_topk_threshold_is_20():
    assert _settings().summary_topk_threshold == 20
```

В `tests/policy/test_policy.py` добавь:

```python
def test_policy_summary_topk_threshold_override():
    from reviewer.config.settings import Settings
    from reviewer.policy.policy import ReviewPolicy
    s = Settings()
    p = ReviewPolicy.load(s, "summary_topk_threshold: 7")
    assert p.summary_topk_threshold == 7


def test_policy_summary_topk_threshold_default_from_settings():
    from reviewer.config.settings import Settings
    from reviewer.policy.policy import ReviewPolicy
    s = Settings()
    assert ReviewPolicy.from_settings(s).summary_topk_threshold == s.summary_topk_threshold
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -k topk_threshold tests/policy/test_policy.py -k summary_topk -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'summary_topk_threshold'` / `'MCPReviewService' object has no attribute '_resolve_summary_topk_threshold'`.

- [ ] **Step 3: Add the settings field**

В `reviewer/config/settings.py`, сразу после блока `summary_cluster_depth` (после строки 72), добавь:

```python
    summary_topk_threshold: int = 20   # порог масштаба для приора сводок (PRI-167): при числе
    # сводок > N query-путь get_subsystem_summaries отдаёт top-k по близости, иначе все; env
    # SUMMARY_TOPK_THRESHOLD; per-repo override в .review.yml
```

- [ ] **Step 4: Add the policy field + read paths**

В `reviewer/policy/policy.py`:

После строки 22 (`summary_cluster_depth: int = 2 ...`) добавь поле:

```python
    summary_topk_threshold: int = 20                            # порог масштаба приора сводок; per-repo override .review.yml (PRI-167)
```

В `from_yaml` (после строки 41 `summary_cluster_depth=int(...)`) добавь в конструктор:

```python
            summary_topk_threshold=int(data.get("summary_topk_threshold", 20)),
```

В `from_settings` (после строки 55 `summary_cluster_depth=settings.summary_cluster_depth,`) добавь:

```python
            summary_topk_threshold=settings.summary_topk_threshold,
```

В `load` (после строки 86, блок `if "summary_cluster_depth" in data:`) добавь:

```python
        if "summary_topk_threshold" in data:
            policy.summary_topk_threshold = int(data["summary_topk_threshold"])
```

- [ ] **Step 5: Add the resolver helper**

В `reviewer/mcp/service.py`, сразу после метода `_resolve_summary_depth` (после строки 360), добавь метод (зеркало `_resolve_summary_depth`):

```python
    def _resolve_summary_topk_threshold(self, repo: str, branch: str) -> tuple[int, str]:
        """Резолв порога масштаба приора сводок: env-дефолт → override из .review.yml ветки.

        repo уже нормализован (вызывается после _resolve_repo_branch). Fail-soft:
        нет токена/ветки/файла/кривой yml → (settings.summary_topk_threshold, "env").
        source = ".review.yml", только если файл явно задаёт ключ summary_topk_threshold."""
        import yaml
        from reviewer.policy.policy import ReviewPolicy
        default = self.settings.summary_topk_threshold
        owner, name = repo.split("/", 1)
        vcs = None
        try:
            vcs = (self._vcs_factory(owner, name) if self._vcs_factory
                   else self._review_service._create_vcs_provider(owner, name))
            text = vcs.get_file_at_ref(".review.yml", branch)
            if not text:
                return default, "env"
            data = yaml.safe_load(text) or {}
            val = ReviewPolicy.load(self.settings, text).summary_topk_threshold
            return val, (".review.yml" if "summary_topk_threshold" in data else "env")
        except Exception:
            log.warning("_resolve_summary_topk_threshold: fail-soft → env-дефолт", exc_info=True)
            return default, "env"
        finally:
            if vcs is not None and self._vcs_factory is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning("_resolve_summary_topk_threshold: не удалось закрыть VCS",
                                exc_info=True)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -k topk_threshold tests/policy/test_policy.py -k summary_topk -q`
Expected: PASS (5 passed).

- [ ] **Step 7: Lint**

Run: `.venv/bin/ruff check reviewer/config/settings.py reviewer/policy/policy.py reviewer/mcp/service.py`
Expected: без новых ошибок в изменённых строках.

- [ ] **Step 8: Commit**

```bash
git add reviewer/config/settings.py reviewer/policy/policy.py reviewer/mcp/service.py \
        tests/mcp/test_subsystem_summaries.py tests/policy/test_policy.py
git commit -m "feat(policy): порог масштаба summary_topk_threshold (env + .review.yml) для приора сводок (PRI-167)"
```

---

## Task 2: Слой хранения — embedding в `SummaryStore` + HNSW-индекс

**Files:**
- Modify: `reviewer/index/summary_store.py:42-56` (`upsert_summary`) + добавить методы после `get_summary` (после строки 118)
- Modify: `reviewer/index/schema.sql:86` (после `CREATE TABLE subsystem_summaries`)
- Test: `tests/index/test_summary_store.py` (новый, integration — нужен Postgres :5433)

**Interfaces:**
- Produces:
  - `SummaryStore.upsert_summary(repo, branch, cluster_key, title, summary, member_node_ids, source_hash, embedding: list[float] | None = None) -> None` (embedding пишется через `COALESCE(EXCLUDED.embedding, existing)` — `None` сохраняет существующий вектор).
  - `SummaryStore.search_summaries(repo, branch, query_embedding: list[float], top_k: int) -> list[dict]` (ANN cosine; shape `{cluster_key, title, summary, updated_at}`; только строки с `embedding IS NOT NULL`).
  - `SummaryStore.count_summaries(repo, branch) -> int`.
  - `SummaryStore.get_pending_embeddings(repo, branch) -> list[dict]` (строки с `embedding IS NULL`: `{cluster_key, title, summary}`).
  - `SummaryStore.set_embedding(repo, branch, cluster_key, embedding: list[float]) -> None`.

- [ ] **Step 1: Write the failing integration test**

Создай `tests/index/test_summary_store.py`:

```python
"""Integration-тесты SummaryStore (PRI-167): требуют Postgres/pgvector на PG_DSN."""
from __future__ import annotations

import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore
from reviewer.index.summary_store import SummaryStore

pytestmark = pytest.mark.integration

DIM = 1024


def _vec(hot: int) -> list[float]:
    """Орт-подобный 1024-вектор с единицей в позиции hot — для предсказуемого ANN."""
    v = [0.0] * DIM
    v[hot] = 1.0
    return v


@pytest.fixture()
def store():
    dsn = Settings().pg_dsn
    ChunkStore(dsn).init_schema()                     # создаёт таблицу + HNSW-индекс
    st = SummaryStore(dsn)
    # чистим тестовый repo до и после
    with st._connect() as conn:
        conn.execute("DELETE FROM subsystem_summaries WHERE repo=%s", ("test/pri167",))
        conn.commit()
    yield st
    with st._connect() as conn:
        conn.execute("DELETE FROM subsystem_summaries WHERE repo=%s", ("test/pri167",))
        conn.commit()
    st.close()


def test_upsert_writes_embedding_and_search_returns_nearest_first(store):
    store.upsert_summary("test/pri167", "dev", "auth", "Авторизация", "...",
                         ["auth/a.py#A"], "h-auth", embedding=_vec(0))
    store.upsert_summary("test/pri167", "dev", "index", "Индекс", "...",
                         ["index/b.py#B"], "h-index", embedding=_vec(500))
    hits = store.search_summaries("test/pri167", "dev", _vec(0), top_k=1)
    assert [h["cluster_key"] for h in hits] == ["auth"]
    assert store.count_summaries("test/pri167", "dev") == 2


def test_upsert_none_embedding_preserves_existing(store):
    store.upsert_summary("test/pri167", "dev", "auth", "Авторизация", "v1",
                         ["auth/a.py#A"], "h1", embedding=_vec(0))
    # повторный upsert с embedding=None не должен обнулить вектор
    store.upsert_summary("test/pri167", "dev", "auth", "Авторизация", "v2",
                         ["auth/a.py#A"], "h1", embedding=None)
    hits = store.search_summaries("test/pri167", "dev", _vec(0), top_k=1)
    assert hits and hits[0]["cluster_key"] == "auth"
    assert hits[0]["summary"] == "v2"          # текст обновился, вектор сохранён


def test_pending_and_set_embedding_backfill(store):
    store.upsert_summary("test/pri167", "dev", "legacy", "Легаси", "...",
                         [], "h-legacy", embedding=None)   # без вектора
    pending = store.get_pending_embeddings("test/pri167", "dev")
    assert [p["cluster_key"] for p in pending] == ["legacy"]
    store.set_embedding("test/pri167", "dev", "legacy", _vec(3))
    assert store.get_pending_embeddings("test/pri167", "dev") == []
    hits = store.search_summaries("test/pri167", "dev", _vec(3), top_k=1)
    assert hits[0]["cluster_key"] == "legacy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose up -d && .venv/bin/pytest tests/index/test_summary_store.py -m integration -q`
Expected: FAIL — `TypeError: upsert_summary() got an unexpected keyword argument 'embedding'` (и отсутствие `search_summaries`/`count_summaries`/`get_pending_embeddings`/`set_embedding`).

- [ ] **Step 3: Add the HNSW index to schema**

В `reviewer/index/schema.sql`, сразу после строки 86 (`);` закрывающей `CREATE TABLE subsystem_summaries`), добавь:

```sql

-- ANN по сводкам подсистем (pgvector HNSW, косинус) — PRI-167
CREATE INDEX IF NOT EXISTS subsystem_summaries_hnsw ON subsystem_summaries
USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

- [ ] **Step 4: Update `upsert_summary` to write embedding (COALESCE)**

В `reviewer/index/summary_store.py`, замени метод `upsert_summary` (строки 42-56) на:

```python
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
```

В импортах `summary_store.py` (строка 12) замени:

```python
from pgvector.psycopg import register_vector
```

на:

```python
from pgvector.psycopg import Vector, register_vector
```

- [ ] **Step 5: Add `search_summaries`, `count_summaries`, `get_pending_embeddings`, `set_embedding`**

В `reviewer/index/summary_store.py`, в конец класса (после `get_summary`, строка 118) добавь:

```python
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
```

- [ ] **Step 6: Run integration test to verify it passes**

Run: `.venv/bin/pytest tests/index/test_summary_store.py -m integration -q`
Expected: PASS (3 passed).

- [ ] **Step 7: Lint**

Run: `.venv/bin/ruff check reviewer/index/summary_store.py`
Expected: чисто на изменённых строках.

- [ ] **Step 8: Commit**

```bash
git add reviewer/index/summary_store.py reviewer/index/schema.sql tests/index/test_summary_store.py
git commit -m "feat(index): эмбеддинг сводок в SummaryStore (upsert/ANN/count/backfill) + HNSW-индекс (PRI-167)"
```

---

## Task 3: Эмбеддинг + дедуп по source_hash в `index_subsystem_summary`

**Files:**
- Modify: `reviewer/mcp/service.py:499-527` (`index_subsystem_summary`)
- Test: `tests/mcp/test_subsystem_summaries.py`

**Interfaces:**
- Consumes: `SummaryStore.upsert_summary(..., embedding=...)` (Task 2); `SummaryStore.get_source_hashes(repo, branch) -> dict[str,str]` (существует, `summary_store.py:71`); `self.components.embedder.embed_documents(list[str]) -> list[list[float]]`.
- Produces: `index_subsystem_summary` пишет эмбеддинг `f"{title}\n{summary}"` только когда `source_hash` кластера изменился (иначе `embedding=None` → COALESCE сохраняет старый вектор, Voyage не дёргается).

- [ ] **Step 1: Write the failing tests**

В `tests/mcp/test_subsystem_summaries.py` добавь:

```python
def test_index_subsystem_summary_embeds_when_hash_changed():
    from reviewer.graph.summaries import compute_source_hash
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    sh = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_source_hashes.return_value = {}          # сводки ещё нет → hash изменился
    c.embedder.embed_documents.return_value = [[0.5, 0.5]]
    svc = _svc(c)
    svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "тело", sh)
    c.embedder.embed_documents.assert_called_once_with(["Индекс\nтело"])
    assert c.summary_store.upsert_summary.call_args.kwargs["embedding"] == [0.5, 0.5]


def test_index_subsystem_summary_dedups_embedding_on_unchanged_hash():
    from reviewer.graph.summaries import compute_source_hash
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    sh = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_source_hashes.return_value = {"reviewer/index": sh}   # хеш совпал
    svc = _svc(c)
    svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "тело", sh)
    c.embedder.embed_documents.assert_not_called()              # Voyage не дёрнут
    assert c.summary_store.upsert_summary.call_args.kwargs["embedding"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -k "embeds_when_hash_changed or dedups_embedding" -q`
Expected: FAIL — `embedder.embed_documents` не вызывается / у `upsert_summary` нет kwarg `embedding`.

- [ ] **Step 3: Update `index_subsystem_summary`**

В `reviewer/mcp/service.py` замени тело `index_subsystem_summary` (строки 507-527, начиная с `from reviewer.graph.summaries import ...`) на:

```python
        from reviewer.graph.summaries import cluster_key as cluster_key_of, compute_source_hash
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"stored": False, "note": rb}
        repo, resolved = rb
        # depth резолвится тем же хелпером, что list_subsystem_clusters без явного depth:
        # cluster_key и source_hash зависят от depth, поэтому совпадение хешей гарантировано
        # только когда кластеры листались тем же дефолтом. При нестандартном depth —
        # fail-soft []+note ниже.
        depth, _ = self._resolve_summary_depth(repo, resolved)
        raw = self.components.store.list_base_members(repo, resolved)
        members = [(f"{p}#{s}", sk) for p, s, _h, _sl, sk in raw
                   if cluster_key_of(p, depth) == cluster_key]
        consistent = compute_source_hash(members) == source_hash
        member_node_ids = sorted(nid for nid, _ in members) if consistent else []
        # Дедуп эмбеддинга по source_hash (PRI-167): пересчитываем вектор только если
        # хеш кластера изменился; иначе embedding=None → COALESCE сохранит старый вектор,
        # Voyage не дёргается. Сбой Voyage → embedding=None + note (бэкфилл доберёт).
        note: str | None = None
        embedding: list[float] | None = None
        stored_hash = self.components.summary_store.get_source_hashes(repo, resolved).get(cluster_key)
        if stored_hash != source_hash:
            try:
                embedding = self.components.embedder.embed_documents([f"{title}\n{summary}"])[0]
            except Exception:
                log.warning("index_subsystem_summary: сбой эмбеддинга — бэкфилл доберёт",
                            exc_info=True)
                note = "эмбеддинг не вычислен (Voyage недоступен) — будет добран бэкфиллом"
        self.components.summary_store.upsert_summary(
            repo, resolved, cluster_key, title, summary, member_node_ids, source_hash,
            embedding=embedding)
        out = {"cluster_key": cluster_key, "stored": True, "members": len(member_node_ids)}
        if not consistent:
            out["note"] = "состав кластера изменился с момента list — member_node_ids не сохранены"
        elif note:
            out["note"] = note
        return out
```

- [ ] **Step 4: Run the full subsystem-summaries unit suite**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -q`
Expected: PASS — новые тесты зелёные; существующие `test_index_and_get_subsystem_summaries_roundtrip_via_store` и `test_index_subsystem_summary_stale_hash_empties_members` по-прежнему зелёные (MagicMock `get_source_hashes` → `.get()` ≠ hash → ветка эмбеддинга; `upsert` фейковый; `out` и `args[5]` не изменились — теперь `embedding` передаётся kwarg'ом, а `member_node_ids` остаётся позиционным `args[5]`).

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check reviewer/mcp/service.py`
Expected: чисто на изменённых строках.

- [ ] **Step 6: Commit**

```bash
git add reviewer/mcp/service.py tests/mcp/test_subsystem_summaries.py
git commit -m "feat(mcp): index_subsystem_summary эмбедит title+summary с дедупом по source_hash (PRI-167)"
```

---

## Task 4: Query-путь в `get_subsystem_summaries` (порог + ANN top-k)

**Files:**
- Modify: `reviewer/mcp/service.py:529-539` (`get_subsystem_summaries`)
- Modify: `tests/mcp/test_subsystem_summaries.py` (`_svc` helper + новые тесты)
- Test: `tests/mcp/test_subsystem_summaries.py`

**Interfaces:**
- Consumes: `_resolve_summary_topk_threshold` (Task 1); `SummaryStore.count_summaries`, `SummaryStore.search_summaries` (Task 2); `self.components.embedder.embed_query(str) -> list[float]`.
- Produces: `get_subsystem_summaries(repo, branch=None, cluster_key=None, query: str | None = None, top_k: int | None = None) -> dict`. С `query` и `count > порога` → `{"summaries": [...top-k...]}`; иначе → `{"summaries": [...all...]}`; `cluster_key` → `{"summary": ...}`. Дефолт `top_k` при ANN — **8**.

- [ ] **Step 1: Extend the `_svc` helper, then write failing tests**

В `tests/mcp/test_subsystem_summaries.py` в функции `_svc` (строки 17-22) добавь стаб резолвера порога — итог:

```python
def _svc(components) -> MCPReviewService:
    svc = MCPReviewService(_settings(), components)
    # изолируем резолв repo/ветки и depth от .env / сети
    svc._resolve_repo_branch = lambda repo, branch: ("o/n", "dev")
    svc._resolve_summary_depth = lambda repo, branch: (2, "env")
    svc._resolve_summary_topk_threshold = lambda repo, branch: (20, "env")
    return svc
```

Добавь тесты:

```python
def test_get_subsystem_summaries_query_above_threshold_returns_topk():
    c = MagicMock()
    c.summary_store.count_summaries.return_value = 25            # > порога 20
    c.summary_store.search_summaries.return_value = [
        {"cluster_key": "auth", "title": "Авторизация", "summary": "...",
         "updated_at": "2026-06-23T00:00:00+00:00"}]
    c.embedder.embed_query.return_value = [0.1, 0.2]
    svc = _svc(c)
    out = svc.get_subsystem_summaries("o/n", "dev", query="как работает логин")
    assert out["summaries"][0]["cluster_key"] == "auth"
    c.embedder.embed_query.assert_called_once_with("как работает логин")
    assert c.summary_store.search_summaries.call_args.args[3] == 8   # top_k по умолчанию
    c.summary_store.get_summaries.assert_not_called()


def test_get_subsystem_summaries_query_below_threshold_returns_all():
    c = MagicMock()
    c.summary_store.count_summaries.return_value = 5             # ≤ порога 20
    c.summary_store.get_summaries.return_value = [
        {"cluster_key": "reviewer/index", "title": "Индекс", "summary": "...",
         "updated_at": "2026-06-23T00:00:00+00:00"}]
    svc = _svc(c)
    out = svc.get_subsystem_summaries("o/n", "dev", query="что угодно")
    assert out["summaries"][0]["cluster_key"] == "reviewer/index"
    c.summary_store.search_summaries.assert_not_called()        # бэк-компат: отдаём все


def test_get_subsystem_summaries_no_query_returns_all_without_counting():
    c = MagicMock()
    c.summary_store.get_summaries.return_value = []
    svc = _svc(c)
    out = svc.get_subsystem_summaries("o/n", "dev")
    assert out == {"summaries": []}
    c.summary_store.search_summaries.assert_not_called()
    c.summary_store.count_summaries.assert_not_called()         # без query порог не считаем
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -k get_subsystem_summaries_query -q`
Expected: FAIL — `get_subsystem_summaries()` не принимает `query` / отдаёт все вместо top-k.

- [ ] **Step 3: Update `get_subsystem_summaries`**

В `reviewer/mcp/service.py` замени метод `get_subsystem_summaries` (строки 529-539) на:

```python
    def get_subsystem_summaries(self, repo: str, branch: str | None = None,
                                cluster_key: str | None = None, query: str | None = None,
                                top_k: int | None = None) -> dict:
        """Дешёвый приор: предрасчитанные summary подсистем (fail-open у потребителя).

        cluster_key → одна сводка. Иначе: при query И числе сводок > порога масштаба
        (SUMMARY_TOPK_THRESHOLD, per-repo .review.yml) — ANN top-k по близости (PRI-167);
        иначе (без query или ≤ порога) — все (бэк-компат)."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"summaries": [], "note": rb}
        repo, resolved = rb
        store = self.components.summary_store
        if cluster_key:
            return {"summary": store.get_summary(repo, resolved, cluster_key)}
        if query:
            threshold, _ = self._resolve_summary_topk_threshold(repo, resolved)
            if store.count_summaries(repo, resolved) > threshold:
                qvec = self.components.embedder.embed_query(query)
                return {"summaries": store.search_summaries(repo, resolved, qvec, top_k or 8)}
        return {"summaries": store.get_summaries(repo, resolved)}
```

- [ ] **Step 4: Run the full subsystem-summaries unit suite**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -q`
Expected: PASS (включая существующий `test_index_and_get_subsystem_summaries_roundtrip_via_store` — он вызывает без `query` → ветка `get_summaries`).

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check reviewer/mcp/service.py`
Expected: чисто.

- [ ] **Step 6: Commit**

```bash
git add reviewer/mcp/service.py tests/mcp/test_subsystem_summaries.py
git commit -m "feat(mcp): get_subsystem_summaries query-путь — ANN top-k при масштабе, иначе все (PRI-167)"
```

---

## Task 5: Серверный self-heal — `backfill_summary_embeddings`

**Files:**
- Modify: `reviewer/mcp/service.py` (добавить метод после `prune_subsystem_summaries`, после строки 559)
- Test: `tests/mcp/test_subsystem_summaries.py`

**Interfaces:**
- Consumes: `SummaryStore.get_pending_embeddings`, `SummaryStore.set_embedding` (Task 2); `self.components.embedder.embed_documents`.
- Produces: `backfill_summary_embeddings(repo, branch=None) -> dict` → `{"embedded": N}` (+ `note`). Эмбедит строки с `embedding IS NULL` из хранимого `title+summary` (без LLM). Идемпотентен. Fail-soft.

- [ ] **Step 1: Write the failing tests**

В `tests/mcp/test_subsystem_summaries.py` добавь:

```python
def test_backfill_summary_embeddings_fills_pending():
    c = MagicMock()
    c.summary_store.get_pending_embeddings.return_value = [
        {"cluster_key": "auth", "title": "Авторизация", "summary": "тело"}]
    c.embedder.embed_documents.return_value = [[0.3, 0.4]]
    svc = _svc(c)
    out = svc.backfill_summary_embeddings("o/n", "dev")
    assert out == {"embedded": 1}
    c.embedder.embed_documents.assert_called_once_with(["Авторизация\nтело"])
    c.summary_store.set_embedding.assert_called_once_with("o/n", "dev", "auth", [0.3, 0.4])


def test_backfill_summary_embeddings_noop_when_none_pending():
    c = MagicMock()
    c.summary_store.get_pending_embeddings.return_value = []
    svc = _svc(c)
    out = svc.backfill_summary_embeddings("o/n", "dev")
    assert out == {"embedded": 0}
    c.embedder.embed_documents.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -k backfill -q`
Expected: FAIL — `'MCPReviewService' object has no attribute 'backfill_summary_embeddings'`.

- [ ] **Step 3: Add the method**

В `reviewer/mcp/service.py`, сразу после метода `prune_subsystem_summaries` (после строки 559), добавь:

```python
    def backfill_summary_embeddings(self, repo: str, branch: str | None = None) -> dict:
        """Self-heal: дозаполнить эмбеддинги сводок с embedding IS NULL из хранимого
        title+summary (без LLM, дедуп по NULL). Идемпотентно: следующий прогон → 0.
        Вызывается /summarize-subsystems после LLM-прохода. Fail-soft (PRI-167)."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"embedded": 0, "note": rb}
        repo, resolved = rb
        store = self.components.summary_store
        pending = store.get_pending_embeddings(repo, resolved)
        if not pending:
            return {"embedded": 0}
        try:
            vecs = self.components.embedder.embed_documents(
                [f"{p['title']}\n{p['summary']}" for p in pending])
        except Exception:
            log.warning("backfill_summary_embeddings: сбой эмбеддинга", exc_info=True)
            return {"embedded": 0, "note": "Voyage недоступен — бэкфилл пропущен"}
        for p, vec in zip(pending, vecs):
            store.set_embedding(repo, resolved, p["cluster_key"], vec)
        return {"embedded": len(pending)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -k backfill -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check reviewer/mcp/service.py`
Expected: чисто.

- [ ] **Step 6: Commit**

```bash
git add reviewer/mcp/service.py tests/mcp/test_subsystem_summaries.py
git commit -m "feat(mcp): backfill_summary_embeddings — серверный self-heal эмбеддингов сводок (PRI-167)"
```

---

## Task 6: MCP-тулы — расширить `get_subsystem_summaries`, зарегистрировать `backfill_summary_embeddings`

**Files:**
- Modify: `reviewer/entrypoints/mcp_server.py:212-219` (`get_subsystem_summaries`) + после `prune_subsystem_summaries` (после строки 228)
- Modify: `tests/mcp/test_server.py:124-128` (набор тулов)
- Test: `tests/mcp/test_server.py`

**Interfaces:**
- Consumes: `service.get_subsystem_summaries(repo, branch, cluster_key, query, top_k)` (Task 4); `service.backfill_summary_embeddings(repo, branch)` (Task 5).
- Produces: MCP-тулы `get_subsystem_summaries(repo, branch?, cluster_key?, query?, top_k?)` и `backfill_summary_embeddings(repo, branch?)`.

- [ ] **Step 1: Update the guard test (failing)**

В `tests/mcp/test_server.py`, в набор ожидаемых тулов (строки 124-127) добавь `backfill_summary_embeddings` — итог блока:

```python
        "list_subsystem_clusters",
        "index_subsystem_summary",
        "get_subsystem_summaries",
        "prune_subsystem_summaries",
        "backfill_summary_embeddings",
    }
```

- [ ] **Step 2: Run the guard test to verify it fails**

Run: `.venv/bin/pytest tests/mcp/test_server.py -k tool -q`
Expected: FAIL — набор зарегистрированных тулов не содержит `backfill_summary_embeddings`.

(Если имя теста неизвестно — запусти весь файл: `.venv/bin/pytest tests/mcp/test_server.py -q` и найди упавший assert набора тулов.)

- [ ] **Step 3: Extend the `get_subsystem_summaries` tool**

В `reviewer/entrypoints/mcp_server.py` замени тул `get_subsystem_summaries` (строки 212-219) на:

```python
    @mcp.tool()
    def get_subsystem_summaries(repo: str, branch: str | None = None,
                                cluster_key: str | None = None,
                                query: str | None = None,
                                top_k: int | None = None) -> dict:
        """Cheap high-level prior for ask / PR-walkthrough: precomputed subsystem
        summaries. cluster_key → one full summary (or null). Otherwise: with `query`
        AND when the summary count exceeds the scale threshold (SUMMARY_TOPK_THRESHOLD,
        per-repo .review.yml), returns the top-k summaries nearest the query by
        embedding; without `query` or at/below the threshold, returns all (back-compat).
        top_k defaults to 8. Empty when none built (consumer is fail-open).
        No PR session; branch defaults to primary."""
        return service.get_subsystem_summaries(repo, branch, cluster_key, query, top_k)
```

- [ ] **Step 4: Register the `backfill_summary_embeddings` tool**

В `reviewer/entrypoints/mcp_server.py`, сразу после тула `prune_subsystem_summaries` (после строки 228), добавь:

```python
    @mcp.tool()
    def backfill_summary_embeddings(repo: str, branch: str | None = None) -> dict:
        """Self-heal: embed any subsystem summaries with a NULL embedding from their
        stored title+summary (no LLM). Idempotent — a later run embeds nothing.
        Called by /reviewer_summarize-subsystems after the LLM pass so older summaries
        become searchable by proximity. Returns {embedded}. No PR session; branch
        defaults to primary."""
        return service.backfill_summary_embeddings(repo, branch)
```

- [ ] **Step 5: Run the guard test to verify it passes**

Run: `.venv/bin/pytest tests/mcp/test_server.py -q`
Expected: PASS.

- [ ] **Step 6: Lint**

Run: `.venv/bin/ruff check reviewer/entrypoints/mcp_server.py`
Expected: чисто.

- [ ] **Step 7: Commit**

```bash
git add reviewer/entrypoints/mcp_server.py tests/mcp/test_server.py
git commit -m "feat(mcp): тулы — get_subsystem_summaries(query,top_k) + backfill_summary_embeddings (PRI-167)"
```

---

## Task 7: Потребители — скиллы ask / pr-walkthrough / summarize-subsystems

**Files:**
- Modify: `plugin/skills/ask/SKILL.md:46-51`
- Modify: `plugin/skills/pr-walkthrough/SKILL.md:38-41`
- Modify: `plugin/skills/summarize-subsystems/SKILL.md:66-72`
- Modify: `tests/skills/test_ask_uses_summaries.py`
- Test: `tests/skills/test_ask_uses_summaries.py`

**Interfaces:**
- Consumes: MCP-тулы `get_subsystem_summaries(..., query=...)` и `backfill_summary_embeddings(...)` (Task 6).
- Produces: скиллы передают вопрос/PR как `query` и триггерят бэкфилл — guard-тесты фиксируют это.

- [ ] **Step 1: Write the failing guard tests**

В `tests/skills/test_ask_uses_summaries.py` добавь:

```python
PRW = Path(__file__).resolve().parents[2] / "plugin" / "skills" / "pr-walkthrough" / "SKILL.md"
SUMM = Path(__file__).resolve().parents[2] / "plugin" / "skills" / "summarize-subsystems" / "SKILL.md"


def test_ask_passes_query_to_summaries():
    text = ASK.read_text(encoding="utf-8")
    assert "get_subsystem_summaries(repo, branch, query=" in text


def test_pr_walkthrough_passes_query_to_summaries():
    text = PRW.read_text(encoding="utf-8")
    assert "query=" in text and "get_subsystem_summaries" in text


def test_summarize_triggers_embedding_backfill():
    text = SUMM.read_text(encoding="utf-8")
    assert "backfill_summary_embeddings" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/skills/test_ask_uses_summaries.py -q`
Expected: FAIL — новые assert'ы не находят `query=` / `backfill_summary_embeddings`.

- [ ] **Step 3: Update `ask/SKILL.md`**

В `plugin/skills/ask/SKILL.md` замени строки 46-48 (начало пункта 1.5) на:

```markdown
1.5. **Subsystem prior (cheap, optional).** Call `get_subsystem_summaries(repo, branch, query="<the user's question>")`.
   At scale (summary count above the deploy threshold) this returns the top-k subsystems nearest the
   question; on small repos it returns all (back-compat). If it returns summaries, use the one matching
   the question's subsystem as a high-level orientation **before** `search_codebase` — this cuts
   exploration steps for architectural / "how does subsystem X work" questions. The summary is only a
   prior: every `path:line` you cite in the answer still comes from real code (`search_codebase` /
   `Read`), never from the summary text.
```

(Оставь следующую строку про fail-open без изменений.)

- [ ] **Step 4: Update `pr-walkthrough/SKILL.md`**

В `plugin/skills/pr-walkthrough/SKILL.md` замени строку 38 на:

```markdown
5. **Subsystem prior (optional).** `get_subsystem_summaries(repo, pr.base_ref, query="<PR title + changed file paths>")` → name the touched
```

(Строки 39-41 — про `pr.base_ref` и fail-open — оставь без изменений; они продолжают этот пункт.)

- [ ] **Step 5: Update `summarize-subsystems/SKILL.md`**

В `plugin/skills/summarize-subsystems/SKILL.md`, между шагом 6 (prune, строки 67-72) и шагом 7 (report, строка 74) вставь новый шаг:

```markdown
6.5. **Backfill summary embeddings (every pass).** Call `backfill_summary_embeddings(repo, branch)` so
   any summaries still missing an embedding (older summaries written before vectorization, or where a
   prior pass's Voyage call failed) become searchable by proximity. It embeds from stored title+summary
   (no LLM), is idempotent (a warm corpus embeds nothing), and is fail-soft. Mention the `embedded`
   count in the report.
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/skills/test_ask_uses_summaries.py -q`
Expected: PASS (5 passed — 2 старых + 3 новых).

- [ ] **Step 7: Commit**

```bash
git add plugin/skills/ask/SKILL.md plugin/skills/pr-walkthrough/SKILL.md \
        plugin/skills/summarize-subsystems/SKILL.md tests/skills/test_ask_uses_summaries.py
git commit -m "feat(skills): ask/pr-walkthrough передают query в приор сводок; summarize дёргает backfill (PRI-167)"
```

---

## Task 8: Полная проверка и финал

**Files:** —

- [ ] **Step 1: Полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: PASS — без регрессий (integration по умолчанию исключены).

- [ ] **Step 2: Integration-прогон (нужен Postgres :5433 + Neo4j)**

Run: `docker compose up -d && .venv/bin/pytest -m integration -q`
Expected: PASS — включая `tests/index/test_summary_store.py`.

- [ ] **Step 3: Линт по проекту (изменённые файлы)**

Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: без новых ошибок (не гнаться за repo-wide clean — фиксируем только введённое этим PR).

- [ ] **Step 4: Дымовой E2E приора (опционально, нужен поднятый индекс)**

Сценарий вручную: на репо с ≤20 сводками `get_subsystem_summaries(repo, branch, query="...")` отдаёт все; при искусственно сниженном `SUMMARY_TOPK_THRESHOLD` (напр. `.review.yml: summary_topk_threshold: 1`) тот же вызов отдаёт top-k. После апгрейда легаси-сводки с `embedding IS NULL` → `backfill_summary_embeddings` заполняет их, повторный вызов → `{"embedded": 0}`.

- [ ] **Step 5: Финальный коммит (если остались несвязанные правки)**

```bash
git add -A && git commit -m "test(pri-167): полный прогон unit+integration — без регрессий" || echo "нечего коммитить"
```

---

## Self-Review (заполнено автором плана)

**Spec coverage:**
- Эмбеддинг на индексе + дедуп по `source_hash` → Task 3 (+ Task 2 хранилище). ✅
- Query-путь `get_subsystem_summaries(query, top_k)` ANN → Task 4 (+ Task 2 `search_summaries`). ✅
- Порог масштаба env + `.review.yml` override → Task 1. ✅
- HNSW-индекс + миграция (idempotent `init_schema`) → Task 2. ✅
- Бэкфилл (self-heal) → Task 5 + триггер в скилле Task 7. ✅
- Потребители (ask, pr-walkthrough, summarize) → Task 7. ✅
- MCP-тулы → Task 6. ✅
- Бэк-компат (no-query/≤порога, guard-тесты) → Task 4 + Task 6 + Task 7. ✅
- Recall (ANN отдаёт ближайшую подсистему) → Task 2 integration-тест. ✅

**Placeholder scan:** нет TBD/TODO; весь код приведён дословно. ✅

**Type consistency:**
- `upsert_summary(..., embedding=None)` — определён в Task 2, передаётся kwarg'ом в Task 3. ✅
- `search_summaries(repo, branch, qvec, top_k)` — позиционно, `top_k` = `args[3]` (проверяется в Task 4 тесте). ✅
- `get_pending_embeddings`/`set_embedding` — Task 2 → Task 5. ✅
- `_resolve_summary_topk_threshold` — Task 1 → Task 4. ✅
- `backfill_summary_embeddings(repo, branch)` — Task 5 → Task 6 (тул) → Task 7 (скилл). ✅
