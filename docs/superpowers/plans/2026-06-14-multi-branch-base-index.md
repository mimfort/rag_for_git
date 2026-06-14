# Multi-Branch Base Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Поддержать несколько отслеживаемых веток (2–3) на репозиторий: каждая ветка получает изолированный base-индекс (вектора + граф), PR ревьюится против индекса своей целевой ветки, PR в неотслеживаемую ветку пропускается.

**Architecture:** Обобщаем дискриминатор `ref` в Postgres: `"base"` → `"base:<branch>"` (консистентно с `"pr:N"`; git запрещает `:` в именах веток). В Neo4j добавляем property `branch` к `:Symbol` с составной уникальностью `(repo, branch, id)`. Имя ветки берётся из `prq.base_ref` (целевая ветка PR, уже известна). Список веток — глобальный allowlist `REVIEW_BRANCHES` в `.env`, первая = первичная.

**Tech Stack:** Python 3.11, pydantic-settings, psycopg (ParadeDB/pgvector/pg_search на :5433), neo4j-driver, Click CLI, FastMCP, pytest (маркер `integration` для тестов на поднятых стораджах).

---

## Стратегия обратной совместимости (читать до начала)

Чтобы каждый коммит оставался зелёным, новые параметры добавляются как **keyword с дефолтом, сохраняющим текущее поведение**:

- **Пустая ветка `branch=""` ≡ legacy.** Хелпер `base_ref("")` → `"base"`, `base_ref("main")` → `"base:main"`. Существующие вызовы без ветки продолжают читать/писать `ref="base"`.
- **Postgres-методы** (`hybrid_search`, `fetch_nodes`) получают `*, base_ref: str = "base"` — дефолт = текущая константа.
- **Neo4j-методы** получают `*, branch: str = ""` — узлы хранят property `branch` (пустая строка для legacy/тестов; match идёт по тому же `branch`, поэтому существующие graph-тесты, создающие и читающие с дефолтом, согласованы).
- **Прод-путь** (`reviewer index`, `review_service.prepare`, self-heal) ВСЕГДА передаёт конкретную ветку. Дефолты — только для legacy-совместимости и unit-фейков.
- **Миграция существующих данных** (Task 15–16) переносит legacy `ref="base"` → `base:<primary>` и проставляет `branch=<primary>` узлам графа с `branch IS NULL`. Выполняется **один раз** после апгрейда; без переэмбеддинга.

## Структура файлов

**Создаются:**
- `reviewer/index/refs.py` — хелпер `base_ref(branch)` (единственный источник соглашения `base:<branch>`).
- `reviewer/services/branch.py` — `resolve_branch(...)` + `current_git_branch(...)` (резолв ветки для ветка-агностичных операций).
- `tests/config/test_review_branches.py`, `tests/index/test_refs.py`, `tests/services/test_branch_resolve.py`, `tests/services/test_routing_branch.py` — новые тесты.

**Модифицируются:** `reviewer/config/settings.py`, `reviewer/index/store.py`, `reviewer/index/freshness.py`, `reviewer/graph/store.py`, `reviewer/retrieval/retriever.py`, `reviewer/tools/code_tools.py`, `reviewer/services/review_service.py`, `reviewer/services/graph_sync.py`, `reviewer/mcp/service.py`, `reviewer/entrypoints/cli.py`, существующие тесты `tests/index/test_freshness.py`, `tests/index/test_store_hybrid.py`, `.env.example`, `README.md`, `CLAUDE.md`, плагин-скилл solve-task в `plugin/`.

---

## Фаза 0 — Конфиг и хелперы (чистый unit, без стораджей)

### Task 1: `Settings.review_branches`

**Files:**
- Modify: `reviewer/config/settings.py:44` (рядом с `default_repo`)
- Test: `tests/config/test_review_branches.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_review_branches.py
from reviewer.config.settings import Settings


def test_review_branches_default_is_main(monkeypatch):
    monkeypatch.delenv("REVIEW_BRANCHES", raising=False)
    s = Settings(_env_file=None)
    assert s.review_branches_list() == ["main"]
    assert s.primary_branch() == "main"


def test_review_branches_parsed_from_csv(monkeypatch):
    monkeypatch.setenv("REVIEW_BRANCHES", "main, master , release/v1")
    s = Settings(_env_file=None)
    assert s.review_branches_list() == ["main", "master", "release/v1"]
    assert s.primary_branch() == "main"


def test_review_branches_empty_falls_back_to_main(monkeypatch):
    monkeypatch.setenv("REVIEW_BRANCHES", "   ")
    s = Settings(_env_file=None)
    assert s.review_branches_list() == ["main"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/config/test_review_branches.py -q`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'review_branches_list'`

- [ ] **Step 3: Add the field and helpers**

В `reviewer/config/settings.py`, после строки `default_repo: str = ""` (строка 44) добавить поле:

```python
    # multi-branch: отслеживаемые ветки (CSV); первая — первичная (дефолт для
    # ветка-агностичных операций: CLI search / solve-task). PR в ветку вне списка
    # ревью пропускает. Пусто = ["main"].
    review_branches: str = "main"
```

И в конце класса, после `review_categories_list` (строка 54), добавить методы:

```python
    def review_branches_list(self) -> list[str]:
        return self._csv(self.review_branches) or ["main"]

    def primary_branch(self) -> str:
        return self.review_branches_list()[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/config/test_review_branches.py -q`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add reviewer/config/settings.py tests/config/test_review_branches.py
git commit -m "feat(config): REVIEW_BRANCHES — список отслеживаемых веток + первичная"
```

---

### Task 2: хелпер `base_ref(branch)`

**Files:**
- Create: `reviewer/index/refs.py`
- Test: `tests/index/test_refs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/index/test_refs.py
from reviewer.index.refs import base_ref


def test_base_ref_empty_is_legacy_base():
    assert base_ref("") == "base"


def test_base_ref_branch_is_namespaced():
    assert base_ref("main") == "base:main"
    assert base_ref("release/v1") == "base:release/v1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/index/test_refs.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'reviewer.index.refs'`

- [ ] **Step 3: Create the helper**

```python
# reviewer/index/refs.py
"""Соглашение об именах ref для base-индекса по ветке.

ref в Postgres — дискриминатор вида `вид:значение`: 'base' / 'base:<branch>' / 'pr:<n>'.
Git запрещает ':' в именах веток, поэтому парсинг 'base:release/v1' однозначен.
Пустая ветка → legacy 'base' (обратная совместимость до миграции).
"""
from __future__ import annotations


def base_ref(branch: str) -> str:
    """Ключ base-индекса ветки: '' → 'base'; 'main' → 'base:main'."""
    return f"base:{branch}" if branch else "base"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/index/test_refs.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add reviewer/index/refs.py tests/index/test_refs.py
git commit -m "feat(index): хелпер base_ref(branch) — соглашение base:<branch>"
```

---

### Task 3: резолвер ветки `resolve_branch`

**Files:**
- Create: `reviewer/services/branch.py`
- Test: `tests/services/test_branch_resolve.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_branch_resolve.py
import pytest
from reviewer.config.settings import Settings
from reviewer.services.branch import resolve_branch


def _settings(csv):
    return Settings(_env_file=None, review_branches=csv)


def test_requested_in_allowlist_used():
    s = _settings("main,master")
    assert resolve_branch("master", "main", s) == "master"


def test_requested_outside_allowlist_raises():
    s = _settings("main,master")
    with pytest.raises(ValueError, match="develop"):
        resolve_branch("develop", "main", s)


def test_current_git_branch_used_when_tracked():
    s = _settings("main,master")
    assert resolve_branch(None, "master", s) == "master"


def test_falls_back_to_primary_when_current_untracked():
    s = _settings("main,master")
    assert resolve_branch(None, "feature/x", s) == "main"


def test_falls_back_to_primary_when_no_current():
    s = _settings("main,master")
    assert resolve_branch(None, None, s) == "main"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/services/test_branch_resolve.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'reviewer.services.branch'`

- [ ] **Step 3: Create the resolver**

```python
# reviewer/services/branch.py
"""Резолв ветки для ветка-агностичных операций (CLI search, solve-task).

Правило: явный запрос (если в allowlist) → текущая git-ветка (если в allowlist)
→ первичная ветка. Граф задач остаётся branch-agnostic — ветвится только код-ретрив.
"""
from __future__ import annotations

import subprocess


def resolve_branch(requested: str | None, current_git_branch: str | None, settings) -> str:
    allow = settings.review_branches_list()
    if requested:
        if requested not in allow:
            raise ValueError(
                f"ветка {requested!r} не в REVIEW_BRANCHES ({allow})"
            )
        return requested
    if current_git_branch and current_git_branch in allow:
        return current_git_branch
    return settings.primary_branch()


def current_git_branch(path: str = ".") -> str | None:
    """Имя текущей git-ветки клона, или None (detached HEAD / не git / ошибка)."""
    try:
        out = subprocess.run(
            ["git", "-C", path, "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    name = out.stdout.strip()
    return name or None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/services/test_branch_resolve.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add reviewer/services/branch.py tests/services/test_branch_resolve.py
git commit -m "feat(services): resolve_branch — текущая ветка/первичная/явный override"
```

---

## Фаза 1 — Слой Postgres (store + freshness)

### Task 4: параметризовать `hybrid_search` / `fetch_nodes` на `base_ref`

**Files:**
- Modify: `reviewer/index/store.py:207-263`
- Test: `tests/index/test_store_hybrid.py` (новый integration-тест изоляции веток)

- [ ] **Step 1: Write the failing test**

Добавить в `tests/index/test_store_hybrid.py`:

```python
@pytest.mark.integration
def test_two_branch_isolation():
    """hybrid_search с разными base_ref изолирует ветки одного репо."""
    s = Settings()
    store = ChunkStore(s.pg_dsn); store.init_schema(); store.clear()
    d = s.embedding_dim
    vec = [0.0] * d; vec[0] = 1.0
    store.upsert([
        _row("base:main", "mod.py", "func", "def func(): return on_main()", vec),
        _row("base:master", "mod.py", "func", "def func(): return on_master()", vec),
    ])
    res_main = store.hybrid_search(
        "a/x", query_text="func", query_embedding=vec,
        overlay_ref="pr:0", changed_paths=[], top_k=10, candidates=20,
        base_ref="base:main",
    )
    texts = {r.text for r in res_main}
    assert any("on_main" in t for t in texts)
    assert not any("on_master" in t for t in texts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/index/test_store_hybrid.py::test_two_branch_isolation -q`
Expected: FAIL — `hybrid_search() got an unexpected keyword argument 'base_ref'`

- [ ] **Step 3: Add `base_ref` parameter**

В `reviewer/index/store.py` заменить сигнатуру и `where` в `hybrid_search` (строки 207-210):

```python
    def hybrid_search(self, repo, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k=20, candidates=50, *, base_ref="base"):
        where = ("repo=%(repo)s AND "
                 "((ref=%(base)s AND NOT (path = ANY(%(changed)s))) OR ref=%(overlay)s)")
```

И добавить `base_ref` в `params` (строка 235-237):

```python
        params = {"repo": repo, "q": _bm25_query(query_text), "vec": Vector(query_embedding),
                  "overlay": overlay_ref, "changed": changed_paths,
                  "cand": candidates, "k": top_k, "base": base_ref}
```

В `fetch_nodes` заменить сигнатуру (строка 244) и WHERE (строка 255) + params (строки 257-258):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/index/test_store_hybrid.py -m integration -q`
Expected: все прежние + `test_two_branch_isolation` — passed (требует поднятый Postgres)

- [ ] **Step 5: Commit**

```bash
git add reviewer/index/store.py tests/index/test_store_hybrid.py
git commit -m "feat(index): hybrid_search/fetch_nodes — параметр base_ref (изоляция веток)"
```

---

### Task 5: cross-branch переиспользование эмбеддингов

**Files:**
- Modify: `reviewer/index/store.py` (новый метод `find_embeddings_by_hashes`)
- Modify: `reviewer/index/freshness.py:16-22` (`_embed_and_upsert` + вызовы)
- Modify: `tests/index/test_freshness.py:8-24` (FakeStore: добавить метод)
- Test: `tests/index/test_freshness.py` (новый unit о reuse)

- [ ] **Step 1: Write the failing test**

Дополнить `FakeStore` в `tests/index/test_freshness.py` (после строки 16 `def existing_hashes`):

```python
    def __init__(self):
        self.rows: list = []
        self.deleted_paths: list[tuple[str, str, list[str]]] = []
        self.deleted_missing: list[tuple[str, str, str, list[str]]] = []
        self.cached_embeddings: dict[str, list[float]] = {}   # content_hash -> vector

    def find_embeddings_by_hashes(self, repo, hashes):
        return {h: self.cached_embeddings[h] for h in hashes if h in self.cached_embeddings}
```

(Замените существующий `__init__`/добавьте метод; `existing_hashes`/`upsert`/`delete_*` оставьте.)

И счётчик вызовов эмбеддера:

```python
class FakeEmb:
    def __init__(self):
        self.calls: list[list[str]] = []
    def embed_documents(self, texts):
        self.calls.append(list(texts))
        return [[0.0]*4 for _ in texts]
```

Новый тест:

```python
def test_update_base_reuses_cached_embedding_across_branches():
    """Чанк с известным content_hash не переэмбеддится — вектор берётся из кэша."""
    from reviewer.index.models import Chunk
    src = "def alpha():\n    pass\n"
    # вычисляем content_hash так же, как chunker
    h = Chunk(path="mod.py", lang="python", symbol_fqn="alpha", kind="function",
              start_line=1, end_line=2, text="def alpha():\n    pass").content_hash
    store, emb = FakeStore(), FakeEmb()
    store.cached_embeddings[h] = [9.0, 9.0, 9.0, 9.0]
    update_base(store, emb, repo="a/x", target_ref="master",
                changed_files=["mod.py"], read=lambda p: src)
    # эмбеддер не звался (единственный чанк взят из кэша)
    assert emb.calls == [] or all(t == [] for t in emb.calls)
    assert any(r.embedding == [9.0, 9.0, 9.0, 9.0] for r in store.rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/index/test_freshness.py::test_update_base_reuses_cached_embedding_across_branches -q`
Expected: FAIL — `_embed_and_upsert` зовёт `embedder.embed_documents` всегда (нет reuse) / `find_embeddings_by_hashes` не вызывается.

- [ ] **Step 3: Add store method + reuse in `_embed_and_upsert`**

В `reviewer/index/store.py` добавить метод (рядом с `existing_hashes`, после строки 124):

```python
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
                "FROM chunks WHERE repo=%s AND content_hash = ANY(%s) AND embedding IS NOT NULL",
                (repo, list(hashes)),
            ).fetchall()
        return {h: list(v) for h, v in rows}
```

В `reviewer/index/freshness.py` заменить `_embed_and_upsert` (строки 16-22) и его вызовы:

```python
def _embed_and_upsert(store, embedder, repo: str, rows: list[ChunkRow]) -> None:
    if not rows:
        return
    # cross-branch reuse: готовые векторы из других ветвей того же репо по content_hash
    cached = store.find_embeddings_by_hashes(repo, [r.content_hash for r in rows])
    to_embed = [r for r in rows if r.content_hash not in cached]
    if to_embed:
        vecs = embedder.embed_documents([r.text for r in to_embed])
        for r, v in zip(to_embed, vecs):
            r.embedding = v
    for r in rows:
        if r.content_hash in cached:
            r.embedding = cached[r.content_hash]
    store.upsert(rows)
```

Обновить два вызова: `build_overlay` (строка 46) и `update_base` (строка 78):

```python
    _embed_and_upsert(store, embedder, repo, batch)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/index/test_freshness.py -q`
Expected: все прежние unit + новый — passed

- [ ] **Step 5: Commit**

```bash
git add reviewer/index/store.py reviewer/index/freshness.py tests/index/test_freshness.py
git commit -m "feat(index): cross-branch reuse эмбеддингов по content_hash (экономия Voyage)"
```

---

### Task 6: `update_base` пишет в `base:<branch>`

**Files:**
- Modify: `reviewer/index/freshness.py:49-78`
- Modify: `tests/index/test_freshness.py` (обновить ассерты ref)

- [ ] **Step 1: Update the existing tests (поведение меняется)**

В `tests/index/test_freshness.py` заменить во всех `update_base(...)`-тестах ожидаемый `"base"` на `"base:main"`/`"base:master"` под `target_ref`. Конкретно:

- `test_update_base_removed_files_calls_delete_paths` (строка 65):
  `assert ("a/x", "base:main", ["old.py"]) in store.deleted_paths`
- `test_update_base_read_none_calls_delete_paths` (строка 95):
  `assert ("a/x", "base:main", ["gone.py"]) in store.deleted_paths`
- `test_update_base_calls_delete_missing_symbols_with_actual_fqns` (строки 117-120):
  `assert ref == "base:main"` (вместо `"base"`)

(У этих тестов `target_ref="main"`, значит ожидаемый ref = `base:main`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/index/test_freshness.py -q`
Expected: FAIL — `update_base` всё ещё пишет литерал `"base"`, ассерты `base:main` не сходятся.

- [ ] **Step 3: Use `base_ref(target_ref)` in `update_base`**

В `reviewer/index/freshness.py` добавить импорт вверху:

```python
from reviewer.index.refs import base_ref
```

Заменить тело `update_base` (строки 58-78) — все `"base"` на `ref`:

```python
def update_base(store, embedder, repo: str, target_ref: str,
                changed_files: list[str],
                read: Callable[[str], str | None],
                removed_files: list[str] | tuple[str, ...] = ()) -> None:
    """Инкрементально обновляет (repo, ref='base:<target_ref>') по изменённым файлам.

    removed_files — пути файлов, удалённых из репо; их чанки вычищаются из индекса.
    Для каждого обработанного файла удаляются символы, исчезнувшие из новой версии.
    """
    ref = base_ref(target_ref)
    py_removed = [p for p in removed_files if p.endswith(".py")]
    store.delete_paths(repo, ref, py_removed)

    seen = store.existing_hashes(repo, ref)
    batch: list[ChunkRow] = []
    for path in changed_files:
        if not path.endswith(".py"):
            continue
        src = read(path)
        if src is None:
            store.delete_paths(repo, ref, [path])
            continue
        rows = _rows_for_file(repo, path, src, ref)
        store.delete_missing_symbols(repo, ref, path, [r.symbol_fqn for r in rows])
        for row in rows:
            if row.content_hash not in seen:
                seen.add(row.content_hash)
                batch.append(row)
    _embed_and_upsert(store, embedder, repo, batch)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/index/test_freshness.py -q`
Expected: все passed

- [ ] **Step 5: Commit**

```bash
git add reviewer/index/freshness.py tests/index/test_freshness.py
git commit -m "feat(index): update_base пишет в base:<target_ref> (изоляция веток)"
```

---

## Фаза 2 — Граф Neo4j (branch-scoped)

### Task 7: `branch` во всех методах GraphStore

**Files:**
- Modify: `reviewer/graph/store.py:21-119`
- Test: `tests/graph/test_graph_branch.py` (новый integration)

- [ ] **Step 1: Write the failing test**

```python
# tests/graph/test_graph_branch.py
import pytest
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore


@pytest.mark.integration
def test_branch_isolation_in_graph():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema()
    g.clear("a/x", branch="main")
    g.clear("a/x", branch="master")
    g.upsert_nodes("a/x", ["mod.py#a", "mod.py#b"], branch="main")
    g.upsert_edges("a/x", [("mod.py#a", "CALLS", "mod.py#b")], branch="main")
    g.upsert_nodes("a/x", ["mod.py#a", "mod.py#c"], branch="master")
    g.upsert_edges("a/x", [("mod.py#a", "CALLS", "mod.py#c")], branch="master")
    try:
        main_rel = g.expand("a/x", ["mod.py#a"], hops=1, branch="main")
        master_rel = g.expand("a/x", ["mod.py#a"], hops=1, branch="master")
        assert "mod.py#b" in main_rel and "mod.py#c" not in main_rel
        assert "mod.py#c" in master_rel and "mod.py#b" not in master_rel
    finally:
        g.clear("a/x", branch="main")
        g.clear("a/x", branch="master")
        g.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/graph/test_graph_branch.py -m integration -q`
Expected: FAIL — `clear()/upsert_nodes()` не принимают `branch`.

- [ ] **Step 3: Add `branch` to all GraphStore methods**

В `reviewer/graph/store.py` заменить `init_schema` constraint (строки 22-27) и все методы. Полностью:

```python
    def init_schema(self) -> None:
        # branch-scoped уникальность: id уникален в пределах (repo, branch).
        self._driver.execute_query("DROP CONSTRAINT sym_id IF EXISTS")
        self._driver.execute_query("DROP CONSTRAINT sym_repo_id IF EXISTS")
        self._driver.execute_query(
            "CREATE CONSTRAINT sym_repo_branch_id IF NOT EXISTS "
            "FOR (s:Symbol) REQUIRE (s.repo, s.branch, s.id) IS UNIQUE")
        self._driver.execute_query(
            "CREATE CONSTRAINT task_key IF NOT EXISTS "
            "FOR (t:Task) REQUIRE t.key IS UNIQUE")
        self._driver.execute_query(
            "CREATE CONSTRAINT pr_id IF NOT EXISTS "
            "FOR (p:PR) REQUIRE p.id IS UNIQUE")
        self._driver.execute_query(
            "CREATE INDEX task_codes IF NOT EXISTS FOR (t:Task) ON (t.codes)")

    def clear(self, repo: str | None = None, *, branch: str | None = None) -> None:
        """Удалить узлы/рёбра: весь граф (repo=None), репо целиком (branch=None),
        или только одну ветку репо (branch задан)."""
        if repo is None:
            self._driver.execute_query("MATCH (n) DETACH DELETE n")
        elif branch is None:
            self._driver.execute_query(
                "MATCH (s:Symbol {repo: $repo}) DETACH DELETE s", repo=repo)
        else:
            self._driver.execute_query(
                "MATCH (s:Symbol {repo: $repo, branch: $branch}) DETACH DELETE s",
                repo=repo, branch=branch)

    def upsert_nodes(self, repo: str, node_ids: list[str], *, branch: str = "") -> None:
        self._driver.execute_query(
            "UNWIND $ids AS id MERGE (:Symbol {repo: $repo, branch: $branch, id: id})",
            ids=list(node_ids), repo=repo, branch=branch)

    def upsert_edges(self, repo: str, edges: list[tuple[str, str, str]], *,
                     branch: str = "") -> None:
        by_rel: dict[str, list[dict]] = {}
        for src, rel, dst in edges:
            by_rel.setdefault(rel, []).append({"src": src, "dst": dst})
        for rel, rows in by_rel.items():
            self._driver.execute_query(
                f"UNWIND $rows AS r "
                f"MATCH (a:Symbol {{repo: $repo, branch: $branch, id: r.src}}) "
                f"MATCH (b:Symbol {{repo: $repo, branch: $branch, id: r.dst}}) "
                f"MERGE (a)-[:{rel}]->(b)",
                rows=rows, repo=repo, branch=branch)

    def expand(self, repo: str, node_ids: list[str], hops: int = 2, *,
               branch: str = "") -> set[str]:
        records, _, _ = self._driver.execute_query(
            f"UNWIND $ids AS sid MATCH (s:Symbol {{repo: $repo, branch: $branch, id: sid}}) "
            f"MATCH (s)-[:CALLS|IMPLEMENTS|TESTED_BY*1..{hops}]-"
            f"(n:Symbol {{repo: $repo, branch: $branch}}) "
            f"RETURN DISTINCT n.id AS id",
            ids=list(node_ids), repo=repo, branch=branch)
        return {r["id"] for r in records}

    def callers(self, repo: str, node_ids: list[str], *, branch: str = "") -> set[str]:
        """Кто вызывает данные символы — направленные входящие CALLS."""
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid "
            "MATCH (c:Symbol {repo: $repo, branch: $branch})-[:CALLS]->"
            "(s:Symbol {repo: $repo, branch: $branch, id: sid}) "
            "RETURN DISTINCT c.id AS id",
            ids=list(node_ids), repo=repo, branch=branch)
        return {r["id"] for r in records}

    def find_symbol(self, repo: str, name: str, *, branch: str = "") -> list[str]:
        """Резолв имени символа в node_id ('path#fqn') в пределах (repo, branch)."""
        records, _, _ = self._driver.execute_query(
            "MATCH (s:Symbol {repo: $repo, branch: $branch}) WHERE s.id CONTAINS $needle "
            "RETURN s.id AS id "
            "ORDER BY (CASE WHEN s.id ENDS WITH $suffix OR s.id ENDS WITH $dotname "
            "THEN 0 ELSE 1 END), s.id "
            "LIMIT 25",
            repo=repo, branch=branch, needle=name, suffix="#" + name, dotname="." + name)
        return [r["id"] for r in records]

    def symbols_for_paths(self, repo: str, paths: list[str], *, branch: str = "") -> set[str]:
        """node_id всех :Symbol (repo, branch), чьи пути в paths (id начинается с 'path#')."""
        if not paths:
            return set()
        prefixes = [p + "#" for p in paths]
        records, _, _ = self._driver.execute_query(
            "MATCH (s:Symbol {repo: $repo, branch: $branch}) "
            "WHERE any(p IN $prefixes WHERE s.id STARTS WITH p) "
            "RETURN s.id AS id",
            repo=repo, branch=branch, prefixes=prefixes)
        return {r["id"] for r in records}

    def delete_symbols(self, repo: str, ids: list[str], *, branch: str = "") -> None:
        """DETACH DELETE перечисленных символов (исчезнувшие/переименованные)."""
        if not ids:
            return
        self._driver.execute_query(
            "UNWIND $ids AS id MATCH (s:Symbol {repo: $repo, branch: $branch, id: id}) "
            "DETACH DELETE s",
            ids=list(ids), repo=repo, branch=branch)

    def delete_outgoing_calls(self, repo: str, ids: list[str], *, branch: str = "") -> None:
        """Снести только ИСХОДЯЩИЕ CALLS у символов (входящие сохраняются)."""
        if not ids:
            return
        self._driver.execute_query(
            "UNWIND $ids AS id "
            "MATCH (s:Symbol {repo: $repo, branch: $branch, id: id})-[r:CALLS]->() DELETE r",
            ids=list(ids), repo=repo, branch=branch)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/graph/test_graph_branch.py -m integration -q`
Expected: passed (требует поднятый Neo4j). Прогнать и существующие graph-тесты: `.venv/bin/pytest tests/graph -m integration -q` — зелёные (используют дефолт `branch=""`).

- [ ] **Step 5: Commit**

```bash
git add reviewer/graph/store.py tests/graph/test_graph_branch.py
git commit -m "feat(graph): branch-property у :Symbol + branch во всех запросах (изоляция веток)"
```

---

## Фаза 3 — Retriever и инструменты

### Task 8: `Retriever` прокидывает branch

**Files:**
- Modify: `reviewer/retrieval/retriever.py:36-91`
- Test: `tests/retrieval/test_retriever_branch.py` (новый unit на фейках)

- [ ] **Step 1: Write the failing test**

```python
# tests/retrieval/test_retriever_branch.py
from reviewer.retrieval.retriever import Retriever


class _Hit:
    def __init__(self, nid):
        self.node_id = nid; self.path = nid.split("#")[0]
        self.symbol_fqn = "f"; self.kind = "function"
        self.start_line = 1; self.end_line = 2; self.text = "code"; self.score = 1.0


class FakeStore:
    def __init__(self): self.calls = []
    def hybrid_search(self, repo, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k=20, candidates=50, *, base_ref="base"):
        self.calls.append(base_ref)
        return [_Hit("a.py#f")]
    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths, *, base_ref="base"):
        return []


class FakeGraph:
    def __init__(self): self.branches = []
    def expand(self, repo, node_ids, hops=2, *, branch=""):
        self.branches.append(branch); return set()


class FakeEmb:
    def embed_query(self, q): return [0.0] * 4


def test_retrieve_passes_base_ref_and_branch():
    store, graph = FakeStore(), FakeGraph()
    r = Retriever(store, graph, FakeEmb(), reranker=None)
    r.retrieve("a/x", "q", ["a.py#f"], overlay_ref="pr:1",
               changed_paths=["a.py"], branch="master")
    assert store.calls == ["base:master"]
    assert graph.branches == ["master"]


def test_search_base_passes_branch():
    store, graph = FakeStore(), FakeGraph()
    r = Retriever(store, graph, FakeEmb(), reranker=None)
    r.search_base("a/x", "q", branch="main")
    assert store.calls[0] == "base:main"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/retrieval/test_retriever_branch.py -q`
Expected: FAIL — `retrieve()` не принимает `branch`.

- [ ] **Step 3: Thread branch through Retriever**

В `reviewer/retrieval/retriever.py` добавить импорт вверху:

```python
from reviewer.index.refs import base_ref
```

Заменить `retrieve` (строки 36-53):

```python
    def retrieve(self, repo, query, changed_node_ids, overlay_ref, changed_paths,
                 top_k=15, candidates=50, *, branch="") -> ContextPack:
        bref = base_ref(branch)
        qvec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(
            repo, query_text=query, query_embedding=qvec, overlay_ref=overlay_ref,
            changed_paths=changed_paths, top_k=candidates, candidates=candidates,
            base_ref=bref)
        related_ids = self.graph.expand(repo, changed_node_ids, hops=2, branch=branch)
        related = self.store.fetch_nodes(repo, list(related_ids), overlay_ref,
                                         changed_paths, base_ref=bref)
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
```

Заменить `search_base` (строки 55-91), прокинув branch в `hybrid_search`/`expand`/`fetch_nodes`:

```python
    def search_base(self, repo, query, top_k=10, candidates=50, *, branch="") -> ContextPack:
        """Гибрид-поиск по base-индексу ветки без PR-сессии — для /solve-task."""
        bref = base_ref(branch)
        qvec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(
            repo, query_text=query, query_embedding=qvec,
            overlay_ref="__none__", changed_paths=[],
            top_k=candidates, candidates=candidates, base_ref=bref)
        merged: dict[str, object] = {}
        for h in hits:
            merged.setdefault(h.node_id, h)
        graph_new = False
        if self.graph is not None and hits:
            try:
                seeds = [h.node_id for h in hits[:top_k]]
                related_ids = self.graph.expand(repo, seeds, hops=1, branch=branch)
                related = self.store.fetch_nodes(repo, list(related_ids), "__none__", [],
                                                 base_ref=bref)
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

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/retrieval/test_retriever_branch.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add reviewer/retrieval/retriever.py tests/retrieval/test_retriever_branch.py
git commit -m "feat(retrieval): Retriever прокидывает branch в store/graph (base:<branch>)"
```

---

### Task 9: `ToolContext.branch` + проброс в инструменты

**Files:**
- Modify: `reviewer/tools/code_tools.py:12-23, 55-125`
- Test: `tests/tools/test_tools_branch.py` (новый unit)

- [ ] **Step 1: Write the failing test**

```python
# tests/tools/test_tools_branch.py
from reviewer.tools.code_tools import ToolContext, make_tools


class FakeRetriever:
    def __init__(self): self.branch = None
    def retrieve(self, repo, query, changed_node_ids, overlay_ref, changed_paths,
                 top_k=8, *, branch=""):
        self.branch = branch
        class P:
            def as_context(self_): return "ctx"
        return P()


class FakeGraph:
    def __init__(self): self.branch = None
    def expand(self, repo, node_ids, hops=2, *, branch=""):
        self.branch = branch; return set()


def test_search_code_passes_branch():
    r = FakeRetriever()
    ctx = ToolContext(retriever=r, graph=FakeGraph(), overlay_ref="pr:1",
                      changed_paths=["a.py"], repo="a/x", branch="master")
    tools = {t.name: t for t in make_tools(ctx)}
    tools["search_code"].invoke({"query": "x"})
    assert r.branch == "master"


def test_get_related_symbols_passes_branch():
    g = FakeGraph()
    ctx = ToolContext(retriever=FakeRetriever(), graph=g, overlay_ref="pr:1",
                      changed_paths=[], repo="a/x", branch="master")
    tools = {t.name: t for t in make_tools(ctx)}
    tools["get_related_symbols"].invoke({"node_id": "a.py#f"})
    assert g.branch == "master"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/tools/test_tools_branch.py -q`
Expected: FAIL — `ToolContext.__init__` не принимает `branch`.

- [ ] **Step 3: Add branch to ToolContext and tool calls**

В `reviewer/tools/code_tools.py` добавить поле в `ToolContext` (после строки 19 `repo: str = ""`):

```python
    branch: str = ""
```

Добавить импорт вверху файла (после строки 8):

```python
from reviewer.index.refs import base_ref
```

Прокинуть `branch=ctx.branch` во все вызовы retriever/graph/store внутри `make_tools`:

- `search_code` (строки 58-60): добавить `branch=ctx.branch` в `ctx.retriever.retrieve(...)`.
- `get_related_symbols` (строка 65): `ctx.graph.expand(ctx.repo, [node_id], hops=2, branch=ctx.branch)`.
- `get_definition` — заменить тело fetch_nodes и retrieve (строки 96-106):

```python
        ids: list[str] = []
        if ctx.graph is not None and hasattr(ctx.graph, "find_symbol"):
            ids = ctx.graph.find_symbol(ctx.repo, symbol, branch=ctx.branch)
        if ids and ctx.store is not None:
            nodes = ctx.store.fetch_nodes(ctx.repo, ids[:3], ctx.overlay_ref,
                                          ctx.changed_paths, base_ref=base_ref(ctx.branch))
            if nodes:
                return "\n\n".join(
                    f"// {n.node_id} ({n.path}:{n.start_line}-{n.end_line})\n{n.text}"
                    for n in nodes)
        pack = ctx.retriever.retrieve(
            ctx.repo, query=symbol, changed_node_ids=ctx.changed_node_ids,
            overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths, top_k=3,
            branch=ctx.branch)
        return pack.as_context() or "(определение не найдено)"
```

- `find_callers` (строка 112): `ctx.graph.callers(ctx.repo, [node_id], branch=ctx.branch)`.

И добавить branch в `ctx_sig` (строка 121-122), чтобы memoize-ключ учитывал ветку:

```python
    ctx_sig = (ctx.repo, ctx.branch, ctx.overlay_ref,
               tuple(sorted(ctx.changed_paths or [])),
               tuple(sorted(ctx.changed_node_ids or [])))
```

Итоговая шапка импортов файла должна включать:

```python
from reviewer.index.refs import base_ref
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/tools/test_tools_branch.py -q`
Expected: 2 passed. Прогнать существующие tool-тесты: `.venv/bin/pytest tests/tools -q` — зелёные (дефолт `branch=""`).

- [ ] **Step 5: Commit**

```bash
git add reviewer/tools/code_tools.py tests/tools/test_tools_branch.py
git commit -m "feat(tools): ToolContext.branch — инструменты ретрива работают в рамках ветки"
```

---

## Фаза 4 — Маршрутизация ревью

### Task 10: `BranchNotTrackedError` + skip в `prepare`

**Files:**
- Modify: `reviewer/services/review_service.py` (новое исключение + ранняя проверка + поле branch)
- Test: `tests/services/test_routing_branch.py` (новый unit с фейковым VCS)

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_routing_branch.py
import pytest
from reviewer.config.settings import Settings
from reviewer.services.review_service import ReviewService, BranchNotTrackedError
from reviewer.vcs.base import PullRequest


class FakeVCS:
    def __init__(self, base_ref):
        self._pr = PullRequest(number=1, base_sha="sha_base", head_sha="sha_head",
                               base_ref=base_ref, title="t", body="")
    def get_pull_request(self, n): return self._pr
    def get_changed_files(self, n): return []
    def get_file_at_ref(self, p, r): return None
    def compare_files(self, a, b): return []
    def close(self): pass


class FakeStore:
    def delete_ref(self, repo, ref): pass
    def get_index_meta(self, repo, ref): return None


class FakeComponents:
    def __init__(self): self.store = FakeStore(); self.graph = None; self.embedder = None


def _svc(csv):
    s = Settings(_env_file=None, review_branches=csv)
    return ReviewService(s, FakeComponents())


def test_untracked_branch_raises_skip():
    svc = _svc("main,master")
    with pytest.raises(BranchNotTrackedError) as exc:
        svc.prepare("a", "x", 1, vcs_provider=FakeVCS("feature/zzz"))
    assert exc.value.branch == "feature/zzz"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/services/test_routing_branch.py -q`
Expected: FAIL — `ImportError: cannot import name 'BranchNotTrackedError'`

- [ ] **Step 3: Add exception, early routing check, and branch field**

В `reviewer/services/review_service.py`:

Добавить исключение после импортов (после строки 28 `log = ...`):

```python
class BranchNotTrackedError(Exception):
    """Целевая ветка PR не в REVIEW_BRANCHES — ревью пропускается."""

    def __init__(self, branch: str) -> None:
        super().__init__(f"ветка '{branch}' не отслеживается (REVIEW_BRANCHES)")
        self.branch = branch
```

Добавить поле в `PreparedReview` (после строки 56 `repo: str ...`):

```python
    branch: str = ""                       # целевая ветка PR (ключ base-индекса)
```

В `prepare`, сразу после `prq = vcs.get_pull_request(pr_number)` (строка 135) добавить раннюю проверку (до дорогих шагов):

```python
            branch = prq.base_ref
            if branch not in self.settings.review_branches_list():
                raise BranchNotTrackedError(branch)
```

Заменить чтение SHA на branch-aware (строка 141):

```python
            from reviewer.index.refs import base_ref as _base_ref
            indexed = self.components.store.get_index_meta(repo, _base_ref(branch))
```

Заменить `set_index_meta` (строка 157):

```python
                    self.components.store.set_index_meta(repo, _base_ref(branch), prq.base_sha)
```

В возврате `PreparedReview(...)` (строка 261) добавить `branch=branch` сразу после `repo=repo`:

```python
            return PreparedReview(
                repo=repo,
                branch=branch,
```

**Важно:** `BranchNotTrackedError` бросается внутри `try`; существующий `except Exception` (строка 277) чистит overlay и пробрасывает — это корректно (skip не оставляет мусора). Но overlay ещё не строился (проверка до `build_overlay`), очистка идемпотентна.

**Fail-soft «индекс ветки не построен» (спека §6.3):** существующая ветка `elif not indexed: log.warning(...)` (строки 188-192) сохраняется без изменений. После правки строки 141 `indexed` читается с `_base_ref(branch)`, поэтому для отслеживаемой, но ещё не проиндексированной ветки `indexed=None` → срабатывает warning, а `build_overlay` строится дальше → ревью идёт на overlay изменённых файлов с ограниченным контекстом. Не падаем.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/services/test_routing_branch.py -q`
Expected: passed

- [ ] **Step 5: Commit**

```bash
git add reviewer/services/review_service.py tests/services/test_routing_branch.py
git commit -m "feat(services): skip ревью при PR в неотслеживаемую ветку + PreparedReview.branch"
```

---

### Task 11: branch-scoped self-heal графа в `prepare`

**Files:**
- Modify: `reviewer/services/review_service.py:159-181` (вызов `patch_graph_incremental`)
- Modify: `reviewer/services/graph_sync.py` (сигнатура `patch_graph_incremental`)
- Test: `tests/services/test_graph_sync_branch.py` (новый unit на фейке графа)

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_graph_sync_branch.py
from reviewer.services.graph_sync import patch_graph_incremental


class FakeGraph:
    def __init__(self):
        self.upsert_branches = []
    def symbols_for_paths(self, repo, paths, *, branch=""):
        return set()
    def delete_symbols(self, repo, ids, *, branch=""):
        pass
    def delete_outgoing_calls(self, repo, ids, *, branch=""):
        pass
    def upsert_nodes(self, repo, ids, *, branch=""):
        self.upsert_branches.append(branch)
    def upsert_edges(self, repo, edges, *, branch=""):
        pass


def test_patch_graph_incremental_uses_branch():
    g = FakeGraph()
    patch_graph_incremental(
        g, "a/x", branch="master",
        changed_sources={"mod.py": "def f():\n    pass\n"}, removed_paths=[])
    assert g.upsert_branches and all(b == "master" for b in g.upsert_branches)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/services/test_graph_sync_branch.py -q`
Expected: FAIL — `patch_graph_incremental()` не принимает `branch`.

- [ ] **Step 3: Add branch to graph_sync and the prepare call**

Заменить `patch_graph_incremental` в `reviewer/services/graph_sync.py` (строки 12-40) — добавить `branch` и прокинуть его во все вызовы графа:

```python
def patch_graph_incremental(graph, repo: str, *, branch: str = "",
                            changed_sources: dict[str, str],
                            removed_paths: list[str]) -> None:
    """Обновить граф (repo, branch) по изменённым/удалённым файлам.

    changed_sources: {path: источник целевой (base) версии} только .py изменённых/
        добавленных файлов — граф досинхронизируется к base-ветке, как и вектора.
    removed_paths: пути удалённых из PR .py-файлов.
    """
    # Удалённые файлы — снести их символы целиком.
    if removed_paths:
        gone = graph.symbols_for_paths(repo, removed_paths, branch=branch)
        graph.delete_symbols(repo, list(gone), branch=branch)

    if not changed_sources:
        return

    nodes, edges = build_graph_from_files(changed_sources)
    changed_paths = list(changed_sources)

    # Снести символы изменённых путей, исчезнувшие из новой версии.
    old = graph.symbols_for_paths(repo, changed_paths, branch=branch)
    stale = old - nodes
    graph.delete_symbols(repo, list(stale), branch=branch)

    # Снести только исходящие CALLS изменённой поверхности (входящие сохраняем),
    # затем переустановить узлы и свежие исходящие рёбра.
    graph.delete_outgoing_calls(repo, list(nodes), branch=branch)
    graph.upsert_nodes(repo, list(nodes), branch=branch)
    graph.upsert_edges(repo, edges, branch=branch)
```

В `reviewer/services/review_service.py` обновить вызов (строки 173-175):

```python
                            patch_graph_incremental(
                                self.components.graph, repo, branch=branch,
                                changed_sources=changed_py, removed_paths=removed_py)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/services/test_graph_sync_branch.py -q`
Expected: passed

- [ ] **Step 5: Commit**

```bash
git add reviewer/services/graph_sync.py reviewer/services/review_service.py tests/services/test_graph_sync_branch.py
git commit -m "feat(services): инкрементальный патч графа в рамках ветки PR"
```

---

### Task 12: MCP `prepare_review` — skip payload + ToolContext branch

**Files:**
- Modify: `reviewer/mcp/service.py:123-171, 233-248`
- Test: `tests/mcp/test_prepare_skip.py` (новый unit)

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/test_prepare_skip.py
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.services.review_service import BranchNotTrackedError


class FakeReviewService:
    def prepare(self, owner, name, pr, vcs_provider=None):
        raise BranchNotTrackedError("feature/zzz")


def test_prepare_review_returns_skip_payload(monkeypatch):
    s = Settings(_env_file=None, review_branches="main,master")

    class Comp:
        graph = None
    svc = MCPReviewService(s, Comp())
    svc._review_service = FakeReviewService()
    out = svc.prepare_review("a/x", 1)
    assert out["status"] == "skipped"
    assert "feature/zzz" in out["reason"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/mcp/test_prepare_skip.py -q`
Expected: FAIL — `prepare_review` не ловит `BranchNotTrackedError`, падает.

- [ ] **Step 3: Catch skip and pass branch to ToolContext**

В `reviewer/mcp/service.py`:

Импорт вверху (после строки 15):

```python
from reviewer.services.review_service import BranchNotTrackedError
```

Обернуть вызов `prepare` в `prepare_review` (строка 137):

```python
        try:
            prepared = self._review_service.prepare(owner, name, pr, vcs_provider=vcs)
        except BranchNotTrackedError as e:
            log.info("Ревью %s#%s пропущено: ветка '%s' не отслеживается",
                     repo, pr, e.branch)
            return {"status": "skipped",
                    "reason": f"branch '{e.branch}' not tracked (REVIEW_BRANCHES)"}
        ctx = self._tool_context(prepared)
```

Добавить `branch=prepared.branch` в `_tool_context` (строка 157-171, в конструктор `ToolContext`):

```python
        return ToolContext(
            retriever=self.components.retriever,
            graph=self.components.graph,
            overlay_ref=prepared.overlay_ref,
            changed_paths=prepared.changed_paths,
            changed_node_ids=prepared.changed_node_ids,
            repo=prepared.repo,
            branch=prepared.branch,
            ...
```

В `search_codebase` (строки 233-248) добавить параметр `branch` и резолв:

```python
    def search_codebase(self, repo: str, query: str, top_k: int = 10,
                        branch: str | None = None) -> str:
        """Гибрид-поиск по base-индексу ветки (без PR-сессии) — для /solve-task."""
        from reviewer.services.repo_id import normalize_repo
        raw = repo or self.settings.default_repo
        if not raw:
            return "(repo не задан: передайте repo или задайте DEFAULT_REPO)"
        try:
            repo = normalize_repo(raw)
        except ValueError:
            return f"(некорректный repo: {raw!r})"
        if branch and branch not in self.settings.review_branches_list():
            return (f"(ветка {branch!r} не в REVIEW_BRANCHES "
                    f"{self.settings.review_branches_list()})")
        resolved = branch or self.settings.primary_branch()
        try:
            pack = self.components.retriever.search_base(
                repo, query, top_k=top_k, branch=resolved)
        except Exception:
            log.warning("search_codebase: сбой поиска", exc_info=True)
            return "(ничего не найдено)"
        return pack.as_context() or "(ничего не найдено)"
```

- [ ] **Step 4: Expose `branch` in the FastMCP `search_codebase` wrapper**

В `reviewer/entrypoints/mcp_server.py` заменить обёртку `search_codebase` (строки 82-87), чтобы скилл solve-task мог передать ветку:

```python
    @mcp.tool()
    def search_codebase(repo: str, query: str, top_k: int = 10,
                        branch: str | None = None) -> str:
        """Hybrid semantic+lexical search over a repo's base code index (no PR session).
        repo is "owner/name" (or "" to use DEFAULT_REPO). branch selects the tracked
        branch's index (default: primary branch from REVIEW_BRANCHES). Use it (e.g. from
        /solve-task) to find relevant existing code by a free-text formulation."""
        return service.search_codebase(repo, query, top_k, branch)
```

(Докстринг обёрнут в `create_server` упоминает «12 тулов» — число тулов не меняется.)

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/mcp/test_prepare_skip.py -q`
Expected: passed. Прогнать существующие mcp-тесты: `.venv/bin/pytest tests/mcp -q` — зелёные.

- [ ] **Step 6: Commit**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_prepare_skip.py
git commit -m "feat(mcp): skip-payload для PR в неотслеживаемую ветку + branch в ToolContext/search_codebase"
```

---

## Фаза 5 — CLI и MCP-экспозиция

### Task 13: `reviewer index` — ветка как ключ хранилища

**Files:**
- Modify: `reviewer/entrypoints/cli.py:122-158`
- Test: `tests/entrypoints/test_cli_index_branch.py` (новый unit через CliRunner с моками)

- [ ] **Step 1: Write the failing test**

```python
# tests/entrypoints/test_cli_index_branch.py
from unittest.mock import MagicMock, patch
from click.testing import CliRunner
from reviewer.entrypoints.cli import cli


@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.build_code_graph", return_value=(["a.py#f"], [], "treesitter"))
@patch("reviewer.entrypoints.cli.rev_parse", return_value="deadbeef")
@patch("reviewer.entrypoints.cli.file_at_ref", return_value="def f(): pass")
@patch("reviewer.entrypoints.cli.list_python_files", return_value=["a.py"])
@patch("reviewer.entrypoints.cli.update_base")
def test_index_stores_under_branch_ref(m_update, m_lpf, m_far, m_rev, m_graph, m_build,
                                       monkeypatch, tmp_path):
    monkeypatch.setenv("DEFAULT_REPO", "a/x")
    monkeypatch.setenv("REVIEW_BRANCHES", "main,master")
    c = MagicMock(); m_build.return_value = c
    runner = CliRunner()
    res = runner.invoke(cli, ["index", str(tmp_path), "--ref", "master"])
    assert res.exit_code == 0, res.output
    # update_base вызван с target_ref="master"
    assert m_update.call_args.args[3] == "master"
    # граф/мета пишутся под ветку master
    c.store.set_index_meta.assert_called_with("a/x", "base:master", "deadbeef")
    c.graph.clear.assert_called_with("a/x", branch="master")
    c.store.delete_paths_except.assert_called_with("a/x", "base:master", ["a.py"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_index_branch.py -q`
Expected: FAIL — index пишет `"base"`, `clear` без branch.

- [ ] **Step 3: Update the `index` command**

В `reviewer/entrypoints/cli.py` добавить импорт вверху (после строки 13):

```python
from reviewer.index.refs import base_ref
```

Заменить команду `index` (строки 122-158):

```python
@cli.command()
@click.argument("repo")
@click.option("--ref", default=None,
              help="git-ref для чтения файлов и ключ ветки; по умолчанию первичная ветка")
@click.option("--branch", "branch_opt", default=None,
              help="имя ветки для хранения индекса; по умолчанию = --ref")
@click.option("--repo", "repo_tag", default=None,
              help="owner/name тег индекса; по умолчанию из git remote origin")
def index(repo: str, ref: str | None, branch_opt: str | None, repo_tag: str | None) -> None:
    """Построить/обновить base-индекс целевой ветки из локального репо."""
    s = Settings()
    c = build_components(s)
    repo_id = _resolve_repo(repo_tag, repo, s)
    ref = ref or s.primary_branch()
    branch = branch_opt or ref
    bref = base_ref(branch)
    try:
        c.store.init_schema()
        files = list_python_files(repo, ref)
        update_base(c.store, c.embedder, repo_id, branch, files,
                    read=lambda p: file_at_ref(repo, p, ref))
        c.store.delete_paths_except(repo_id, bref, files)
        sha = rev_parse(repo, ref)
        c.store.set_index_meta(repo_id, bref, sha)
        # --- граф кода (в рамках ветки) ---
        src_by_path = {p: file_at_ref(repo, p, ref) for p in files}
        src_by_path = {p: v for p, v in src_by_path.items() if v is not None}
        gnodes, gedges, backend = build_code_graph(
            repo, ref, files, src_by_path, s.graph_backend,
        )
        c.graph.init_schema()
        c.graph.clear(repo_id, branch=branch)   # rebuild только этой ветки репо
        c.graph.upsert_nodes(repo_id, list(gnodes), branch=branch)
        c.graph.upsert_edges(repo_id, gedges, branch=branch)
        click.echo(
            f"Проиндексировано [{repo_id}@{branch}] файлов: {len(files)} @ {sha[:7]}; "
            f"граф [{backend}]: узлов {len(gnodes)}, рёбер {len(gedges)}"
        )
    finally:
        c.store.close()
        if c.graph:
            c.graph.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_index_branch.py -q`
Expected: passed

- [ ] **Step 5: Commit**

```bash
git add reviewer/entrypoints/cli.py tests/entrypoints/test_cli_index_branch.py
git commit -m "feat(cli): reviewer index хранит индекс/граф под целевой веткой (base:<branch>)"
```

---

### Task 14: `reviewer search` — `--branch`

**Files:**
- Modify: `reviewer/entrypoints/cli.py:160-182`
- Test: `tests/entrypoints/test_cli_search_branch.py` (новый unit)

- [ ] **Step 1: Write the failing test**

```python
# tests/entrypoints/test_cli_search_branch.py
from unittest.mock import MagicMock, patch
from click.testing import CliRunner
from reviewer.entrypoints.cli import cli


@patch("reviewer.entrypoints.cli.build_components")
def test_search_passes_base_ref_for_branch(m_build, monkeypatch):
    monkeypatch.setenv("DEFAULT_REPO", "a/x")
    monkeypatch.setenv("REVIEW_BRANCHES", "main,master")
    c = MagicMock()
    c.embedder.embed_query.return_value = [0.0] * 4
    c.store.hybrid_search.return_value = []
    m_build.return_value = c
    runner = CliRunner()
    res = runner.invoke(cli, ["search", "token", "--branch", "master"])
    assert res.exit_code == 0, res.output
    assert c.store.hybrid_search.call_args.kwargs["base_ref"] == "base:master"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_search_branch.py -q`
Expected: FAIL — `search` не имеет `--branch`, `hybrid_search` зовётся без `base_ref`.

- [ ] **Step 3: Update the `search` command**

Заменить команду `search` (строки 160-182):

```python
@cli.command()
@click.argument("query")
@click.option("--repo", "repo_tag", default=None, help="owner/name; по умолчанию DEFAULT_REPO")
@click.option("--branch", "branch_opt", default=None,
              help="ветка base-индекса; по умолчанию первичная (REVIEW_BRANCHES)")
def search(query: str, repo_tag: str | None, branch_opt: str | None) -> None:
    """Гибридный поиск по base-индексу ветки (диагностика)."""
    from reviewer.services.repo_id import normalize_repo
    s = Settings()
    repo_id = normalize_repo(repo_tag or s.default_repo) if (repo_tag or s.default_repo) else None
    if repo_id is None:
        raise click.ClickException("Укажите --repo owner/name (или DEFAULT_REPO в .env)")
    if branch_opt and branch_opt not in s.review_branches_list():
        raise click.ClickException(
            f"Ветка {branch_opt!r} не в REVIEW_BRANCHES ({s.review_branches_list()})")
    branch = branch_opt or s.primary_branch()
    c = build_components(s)
    try:
        qvec = c.embedder.embed_query(query)
        hits = c.store.hybrid_search(
            repo_id, query_text=query, query_embedding=qvec,
            overlay_ref="", changed_paths=[], top_k=10, base_ref=base_ref(branch),
        )
        for h in hits:
            click.echo(f"{h.score:.3f}  {h.node_id}  ({h.path}:{h.start_line})")
    finally:
        c.store.close()
        if c.graph:
            c.graph.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_search_branch.py -q`
Expected: passed

- [ ] **Step 5: Commit**

```bash
git add reviewer/entrypoints/cli.py tests/entrypoints/test_cli_search_branch.py
git commit -m "feat(cli): reviewer search --branch (поиск по base-индексу конкретной ветки)"
```

---

## Фаза 6 — Миграция legacy-данных

### Task 15: миграция Postgres `ref='base'` → `base:<primary>`

**Files:**
- Modify: `reviewer/index/store.py` (метод `migrate_legacy_base`)
- Test: `tests/index/test_migrate_base.py` (новый integration)

- [ ] **Step 1: Write the failing test**

```python
# tests/index/test_migrate_base.py
import psycopg
import pytest
from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore, ChunkRow


def _row(ref, path, fqn):
    return ChunkRow(repo="a/x", ref=ref, content_hash=fqn, path=path, lang="python",
                    symbol_fqn=fqn, kind="function", start_line=1, end_line=2,
                    text="code", embedding=[0.0] * 1024)


@pytest.mark.integration
def test_migrate_legacy_base_to_primary():
    s = Settings()
    store = ChunkStore(s.pg_dsn); store.init_schema(); store.clear()
    store.upsert([_row("base", "a.py", "f")])
    store.set_index_meta("a/x", "base", "deadbeef")
    store.migrate_legacy_base("main")
    with psycopg.connect(s.pg_dsn) as conn:
        refs = {r[0] for r in conn.execute("SELECT DISTINCT ref FROM chunks").fetchall()}
        meta = conn.execute("SELECT ref FROM index_meta WHERE repo='a/x'").fetchone()
    assert refs == {"base:main"}
    assert meta == ("base:main",)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/index/test_migrate_base.py -m integration -q`
Expected: FAIL — `'ChunkStore' object has no attribute 'migrate_legacy_base'`

- [ ] **Step 3: Add migration method**

В `reviewer/index/store.py` добавить (после `set_index_meta`, строка 158):

```python
    def migrate_legacy_base(self, primary: str) -> int:
        """Перенести legacy ref='base' → 'base:<primary>' в chunks и index_meta.

        Идемпотентно: повторный вызов — no-op (legacy 'base' уже отсутствует).
        Без переэмбеддинга — векторы сохраняются. Выполнять один раз после апгрейда.
        """
        target = f"base:{primary}"
        with self._connect() as conn:
            cur = conn.execute("UPDATE chunks SET ref=%s WHERE ref='base'", (target,))
            n = cur.rowcount
            conn.execute("UPDATE index_meta SET ref=%s WHERE ref='base'", (target,))
            conn.commit()
        return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/index/test_migrate_base.py -m integration -q`
Expected: passed (требует Postgres)

- [ ] **Step 5: Commit**

```bash
git add reviewer/index/store.py tests/index/test_migrate_base.py
git commit -m "feat(index): migrate_legacy_base — перенос base → base:<primary> без переэмбеддинга"
```

---

### Task 16: миграция графа + CLI `migrate-branches`

**Files:**
- Modify: `reviewer/graph/store.py` (метод `migrate_legacy_branch`)
- Modify: `reviewer/entrypoints/cli.py` (команда `migrate-branches`)
- Test: `tests/graph/test_migrate_branch.py` (integration) + `tests/entrypoints/test_cli_migrate.py` (unit)

- [ ] **Step 1: Write the failing tests**

```python
# tests/graph/test_migrate_branch.py
import pytest
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore


@pytest.mark.integration
def test_migrate_legacy_branch_sets_primary():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    # создать legacy-узел без branch (как старый upsert)
    g._driver.execute_query("MERGE (:Symbol {repo: 'a/x', id: 'legacy.py#f'})")
    try:
        g.migrate_legacy_branch("main")
        rec, _, _ = g._driver.execute_query(
            "MATCH (s:Symbol {repo:'a/x', id:'legacy.py#f'}) RETURN s.branch AS b")
        assert rec[0]["b"] == "main"
    finally:
        g._driver.execute_query("MATCH (s:Symbol {repo:'a/x', id:'legacy.py#f'}) DETACH DELETE s")
        g.close()
```

```python
# tests/entrypoints/test_cli_migrate.py
from unittest.mock import MagicMock, patch
from click.testing import CliRunner
from reviewer.entrypoints.cli import cli


@patch("reviewer.entrypoints.cli.build_components")
def test_migrate_branches_calls_store_and_graph(m_build, monkeypatch):
    monkeypatch.setenv("REVIEW_BRANCHES", "main,master")
    c = MagicMock(); c.store.migrate_legacy_base.return_value = 5
    m_build.return_value = c
    res = CliRunner().invoke(cli, ["migrate-branches"])
    assert res.exit_code == 0, res.output
    c.store.migrate_legacy_base.assert_called_with("main")
    c.graph.migrate_legacy_branch.assert_called_with("main")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_migrate.py tests/graph/test_migrate_branch.py -q`
Expected: FAIL — нет метода `migrate_legacy_branch` и команды `migrate-branches`.

- [ ] **Step 3: Add graph migration + CLI command**

В `reviewer/graph/store.py` добавить (после `init_schema`, строка 37):

```python
    def migrate_legacy_branch(self, primary: str) -> None:
        """Проставить branch=<primary> символам без ветки (legacy-узлы). Идемпотентно."""
        self._driver.execute_query(
            "MATCH (s:Symbol) WHERE s.branch IS NULL SET s.branch = $primary",
            primary=primary)
```

В `reviewer/entrypoints/cli.py` добавить команду (после `index`, до `search`):

```python
@cli.command("migrate-branches")
def migrate_branches() -> None:
    """Один раз после апгрейда: перенести legacy base-индекс на первичную ветку."""
    s = Settings()
    c = build_components(s)
    primary = s.primary_branch()
    try:
        c.store.init_schema()
        n = c.store.migrate_legacy_base(primary)
        if c.graph is not None:
            c.graph.init_schema()
            c.graph.migrate_legacy_branch(primary)
        click.echo(f"Миграция завершена: {n} чанков → base:{primary}; граф → branch={primary}")
    finally:
        c.store.close()
        if c.graph:
            c.graph.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_migrate.py -q` (unit) и `.venv/bin/pytest tests/graph/test_migrate_branch.py -m integration -q` (integration)
Expected: passed

- [ ] **Step 5: Commit**

```bash
git add reviewer/graph/store.py reviewer/entrypoints/cli.py tests/graph/test_migrate_branch.py tests/entrypoints/test_cli_migrate.py
git commit -m "feat: команда migrate-branches — перенос legacy графа/индекса на первичную ветку"
```

---

## Фаза 7 — Скилл solve-task и документация

### Task 17: скилл solve-task передаёт текущую ветку

**Files:**
- Modify: скилл solve-task в `plugin/` (найти: `grep -rl "solve-task\|search_codebase" plugin/`)

- [ ] **Step 1: Найти и прочитать инструкцию скилла**

Run: `grep -rln "search_codebase" plugin/`
Открыть найденный `SKILL.md` (или аналог).

- [ ] **Step 2: Дополнить инструкцию резолвом ветки**

В секцию, где скилл вызывает `search_codebase`, добавить текст (русским, в стиле существующих инструкций):

```markdown
**Ветка кодовой базы.** Перед `search_codebase` определи текущую git-ветку проекта:
`git branch --show-current`. Если она входит в `REVIEW_BRANCHES` — ветвимся
относительно неё: передавай её в `search_codebase` параметром `branch`. Если
пользователь явно указал, «от какой ветки ветвимся» — используй её. Иначе не
передавай `branch` (сервер возьмёт первичную ветку).
```

Это документация (LLM-инструкция) — автоматического теста нет; проверяется code review.

- [ ] **Step 3: Commit**

```bash
git add plugin/
git commit -m "docs(plugin): solve-task ветвится относительно текущей git-ветки (search_codebase branch)"
```

---

### Task 18: `.env.example` + README + CLAUDE.md

**Files:**
- Modify: `.env.example` (секция мульти-репо)
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: Update `.env.example`**

После секции `DEFAULT_REPO=` (мульти-репо) добавить:

```env
# Отслеживаемые ветки для ревью (CSV). Первая — первичная (дефолт для
# `reviewer index --ref`, CLI search, solve-task). PR в ветку вне этого
# списка ревью пропускает. Пусто = main.
REVIEW_BRANCHES=main,master
```

- [ ] **Step 2: Update README.md и CLAUDE.md**

В `CLAUDE.md`, в раздел «Неочевидные факты», добавить пункт:

```markdown
- **Мульти-бранч base-индекс.** Каждая отслеживаемая ветка (`REVIEW_BRANCHES`, CSV,
  первая — первичная) имеет изолированный base-индекс: в Postgres `ref="base:<branch>"`
  (overlay PR остаётся `pr:N`), в Neo4j `:Symbol{repo, branch, id}`. PR ревьюится против
  индекса своей целевой ветки (`prq.base_ref`); PR в ветку вне списка ревью **пропускает**
  (`prepare_review` → `{status:"skipped"}`). `reviewer index --ref <branch>` строит индекс
  ветки. Эмбеддинги переиспользуются между ветками по `content_hash` (экономия Voyage).
  Ветка-агностичные операции (CLI search, solve-task) идут по первичной ветке или текущей
  git-ветке клона. Миграция legacy-данных: `reviewer migrate-branches` (один раз).
```

В `README.md` (раздел про индексацию / `.env`) добавить аналогичное описание `REVIEW_BRANCHES` и команду `reviewer index --ref master` для второй ветки + `reviewer migrate-branches`.

- [ ] **Step 3: Commit**

```bash
git add .env.example README.md CLAUDE.md
git commit -m "docs: REVIEW_BRANCHES, мульти-бранч индекс, migrate-branches"
```

---

## Финальная верификация

- [ ] **Прогнать весь unit-набор:**

Run: `.venv/bin/pytest -q`
Expected: все unit зелёные (integration исключены по умолчанию).

- [ ] **Прогнать integration (нужны Postgres+Neo4j+Voyage):**

Run: `docker compose up -d && .venv/bin/pytest -m integration -q`
Expected: зелёные, включая `test_two_branch_isolation`, `test_branch_isolation_in_graph`, миграции.

- [ ] **Линт:**

Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: без новых ошибок в изменённых файлах (см. memory: на main ruff не идеально чист — не гнаться за repo-wide clean, проверять только затронутое).

- [ ] **Ручной смоук (две ветки):**

```bash
reviewer index /path/to/repo --ref main
reviewer index /path/to/repo --ref master
reviewer search "token verification" --branch master
```
Expected: индекс строится под обе ветки; поиск по master возвращает master-версии.
```
