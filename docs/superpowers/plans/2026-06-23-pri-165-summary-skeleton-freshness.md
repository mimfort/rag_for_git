# PRI-165 — Freshness сводок по структуре + cap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ключ свежести сводок подсистем считать от структурного скелета символов (`skeleton_hash` на лету), а не от полного `content_hash`, + server-side cap пересборок + выбор модели в скилле — чтобы body-only правки не гнали LLM-пересборку.

**Architecture:** Новый чистый хелпер `symbol_skeleton_hash` (tree-sitter, в `chunker.py`) хэширует только строки скелета символа. `ChunkStore.list_base_members` отдаёт его 5-м элементом кортежа (на лету из `text`, без миграции). `build_clusters` и `index_subsystem_summary` подают `skeleton_hash` в `compute_source_hash` (синхронно). `list_subsystem_clusters` применяет cap (порядок: без сводки → старейшие `updated_at`) и отдаёт `deferred`. Скилл `summarize-subsystems` репортит `deferred` и спрашивает дешёвую модель для генерации.

**Tech Stack:** Python 3.11–3.13, tree-sitter-python, pydantic-settings, psycopg/pgvector (ParadeDB :5433), pytest, FastMCP. Граф — Neo4j (не затрагивается).

## Global Constraints

- **Язык проекта — русский:** все новые комментарии, докстринги, тексты — на русском. Тело `SKILL.md` — на английском (токены), но инструктирует отвечать пользователю по-русски.
- **Коммиты:** Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude). Ветка: `feat/pri-165-summary-skeleton-freshness` (уже создана от `dev`), PR → `dev`.
- **TDD:** сначала падающий тест, затем минимальная реализация.
- **Линт:** `ruff check .` (line-length 100, target py311). Прогонять перед коммитом по затронутым файлам.
- **Тесты по умолчанию исключают `integration`** (`addopts = -m 'not integration'`). Integration-тесты требуют поднятых Postgres/Neo4j: `docker compose up -d`.
- **Инвариант:** `symbol_skeleton_hash` хэширует **нормализованный текст** строк скелета (`rstrip`, как `content_hash`), не их номера → позиция/порядок символа в файл не протекают в хэш.
- **Спека:** `docs/superpowers/specs/2026-06-23-pri-165-summary-skeleton-freshness-design.md`.

---

### Task 1: `symbol_skeleton_hash` — хэш структурного скелета символа

**Files:**
- Modify: `reviewer/index/chunker.py` (добавить `import hashlib` + функцию после `python_skeleton`, ~строка 90)
- Test: `tests/index/test_chunker.py`

**Interfaces:**
- Consumes: существующий `python_skeleton(source: bytes) -> list[int]` (`chunker.py:46`).
- Produces: `symbol_skeleton_hash(text: str) -> str` — sha256 нормализованного текста строк скелета символа; позиционно-независим; fallback на полный текст при пустом скелете. Потребляется в Task 2 (`store.list_base_members`).

- [ ] **Step 1: Написать падающие тесты**

В конец `tests/index/test_chunker.py` добавить (импорт расширить в строке 1):

```python
from reviewer.index.chunker import chunk_python, python_skeleton, symbol_skeleton_hash

_FN = (
    'def search(query: str, top_k: int = 10) -> list:\n'
    '    """Поиск по индексу."""\n'
    '    return rrf(query)[:top_k]\n'
)


def test_skeleton_hash_ignores_body_change():
    body = _FN.replace("rrf(query)[:top_k]", "rrf(query, k=60)[:top_k]")
    assert symbol_skeleton_hash(_FN) == symbol_skeleton_hash(body)   # тело не влияет


def test_skeleton_hash_changes_on_signature():
    sig = _FN.replace("top_k: int = 10)", "top_k: int = 10, *, rerank: bool = True)")
    assert symbol_skeleton_hash(_FN) != symbol_skeleton_hash(sig)    # сигнатура влияет


def test_skeleton_hash_changes_on_docstring_first_line():
    doc = _FN.replace('"""Поиск по индексу."""', '"""Другое описание."""')
    assert symbol_skeleton_hash(_FN) != symbol_skeleton_hash(doc)


def test_skeleton_hash_is_position_independent():
    shifted = "\n\n" + _FN                                           # сдвиг символа вниз
    assert symbol_skeleton_hash(_FN) == symbol_skeleton_hash(shifted)


def test_skeleton_hash_fallback_without_definitions():
    # нет def/class → fallback на нормализованный полный текст (детерминирован, реагирует на правку)
    assert symbol_skeleton_hash("X = 1\n") == symbol_skeleton_hash("X = 1")
    assert symbol_skeleton_hash("X = 1\n") != symbol_skeleton_hash("X = 2\n")
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `.venv/bin/pytest tests/index/test_chunker.py -k skeleton_hash -q`
Expected: FAIL — `ImportError: cannot import name 'symbol_skeleton_hash'`.

- [ ] **Step 3: Реализовать функцию**

В `reviewer/index/chunker.py`: добавить в начало `import hashlib` (рядом с существующими импортами, строки 1–4), затем после `python_skeleton` (после строки 90) добавить:

```python
def symbol_skeleton_hash(text: str) -> str:
    """Хэш структурного скелета символа: сигнатуры def/class (до ':') + 1-я строка docstring.

    Ключ свежести сводок подсистем по структуре (PRI-165): меняется при смене сигнатуры/
    docstring, но НЕ при правке тела. Хэшируется ТЕКСТ строк скелета (rstrip, как content_hash),
    а не их номера, поэтому сдвиг/реордеринг символа в файл не протекают в хэш. Пустой скелет
    (битый код / нет определений) → fallback на нормализованный полный текст (безопасно: любая
    правка ре-стейлит)."""
    nums = python_skeleton(text.encode("utf-8"))      # 1-based строки скелета относительно text
    lines = text.splitlines()
    if nums:
        body = "\n".join(lines[n - 1].rstrip() for n in nums if 1 <= n <= len(lines))
    else:
        body = "\n".join(ln.rstrip() for ln in lines)
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Запустить — убедиться, что проходят**

Run: `.venv/bin/pytest tests/index/test_chunker.py -q`
Expected: PASS (все, включая существующие `python_skeleton`-тесты).

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/index/chunker.py tests/index/test_chunker.py
git add reviewer/index/chunker.py tests/index/test_chunker.py
git commit -m "feat(index): symbol_skeleton_hash — хэш структурного скелета символа (PRI-165)"
```

---

### Task 2: `list_base_members` отдаёт `skeleton_hash`; `SummaryStore.get_updated_ats`

**Files:**
- Modify: `reviewer/index/store.py:202-210` (`list_base_members`)
- Modify: `reviewer/index/summary_store.py` (добавить `get_updated_ats` после `get_source_hashes`, ~строка 65)
- Test: `tests/index/test_summary_store.py` (integration; `pytestmark = pytest.mark.integration`)

**Interfaces:**
- Consumes: `symbol_skeleton_hash(text)` (Task 1); существующий `reviewer.index.refs.base_ref`.
- Produces:
  - `ChunkStore.list_base_members(repo, branch) -> list[tuple[path, fqn, content_hash, start_line, skeleton_hash]]` (5-кортеж; позиции 0–3 без изменений).
  - `SummaryStore.get_updated_ats(repo, branch) -> dict[str, datetime]` — `updated_at` по `cluster_key`; нет таблицы → `{}`. Потребляются в Task 3 (`service`).

**Примечание о порядке:** этот таск — продюсер 5-кортежа. До Task 3 `service` ещё распаковывает 4-кортеж, поэтому **реальный** `list_subsystem_clusters` между Task 2 и Task 3 несогласован. Unit-suite остаётся зелёным (service-тесты мокают `list_base_members`); согласованность реального пути закрывает Task 3. Не запускать реальный MCP между Task 2 и Task 3.

- [ ] **Step 1: Обновить integration-тесты (падающие)**

В `tests/index/test_summary_store.py`: расширить `test_list_base_members_reads_base_ref_rows` и добавить тест `get_updated_ats`:

```python
def test_list_base_members_reads_base_ref_rows():
    from reviewer.index.store import ChunkStore, ChunkRow
    from reviewer.index.chunker import symbol_skeleton_hash
    cs = ChunkStore(DSN)
    cs.init_schema()
    cs.upsert([ChunkRow(repo="t/t", ref="base:dev", content_hash="h", path="reviewer/x/a.py",
                        lang="python", symbol_fqn="A", kind="function",
                        start_line=3, end_line=9, text="def a(): ...", embedding=[0.0]*1024)])
    try:
        members = cs.list_base_members("t/t", "dev")
        # 5-кортеж: skeleton_hash считается на лету из text
        assert ("reviewer/x/a.py", "A", "h", 3, symbol_skeleton_hash("def a(): ...")) in members
    finally:
        cs.delete_ref("t/t", "base:dev")
        cs.close()


def test_get_updated_ats_returns_datetime_per_cluster(store):
    from datetime import datetime
    store.upsert_summary("t/t", "dev", "reviewer/index", "A", "s", [], "h1")
    ats = store.get_updated_ats("t/t", "dev")
    assert "reviewer/index" in ats
    assert isinstance(ats["reviewer/index"], datetime)   # сырой datetime, не isoformat
```

- [ ] **Step 2: Запустить — убедиться, что падают** (нужен Postgres: `docker compose up -d`)

Run: `.venv/bin/pytest -m integration tests/index/test_summary_store.py -q`
Expected: FAIL — `test_list_base_members...` падает на 4-кортеже vs 5-кортеж; `test_get_updated_ats...` падает на `AttributeError: get_updated_ats`.

- [ ] **Step 3: Реализовать `list_base_members` (5-кортеж)**

Заменить `reviewer/index/store.py:202-210` на:

```python
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
```

- [ ] **Step 4: Реализовать `get_updated_ats`**

В `reviewer/index/summary_store.py` после `get_source_hashes` (после строки 65) добавить:

```python
    def get_updated_ats(self, repo: str, branch: str) -> dict[str, "datetime"]:
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
```

Добавить импорт для аннотации в начало файла (после строки 8 `import threading`):

```python
from datetime import datetime
```

И заменить аннотацию возврата на `dict[str, datetime]` (убрать кавычки вокруг `datetime`).

- [ ] **Step 5: Запустить — убедиться, что проходят**

Run: `.venv/bin/pytest -m integration tests/index/test_summary_store.py -q`
Expected: PASS.

- [ ] **Step 6: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/index/store.py reviewer/index/summary_store.py tests/index/test_summary_store.py
git add reviewer/index/store.py reviewer/index/summary_store.py tests/index/test_summary_store.py
git commit -m "feat(index): list_base_members отдаёт skeleton_hash + SummaryStore.get_updated_ats (PRI-165)"
```

---

### Task 3: freshness по `skeleton_hash` в `summaries`/`service` + cap

**Files:**
- Modify: `reviewer/graph/summaries.py` (`Member` :14-19, `compute_source_hash` :44-50 докстринг, `build_clusters` :87)
- Modify: `reviewer/config/settings.py:71` (добавить поле после `summary_cluster_depth`)
- Modify: `reviewer/mcp/service.py` (`list_subsystem_clusters` :419-447, `index_subsystem_summary` :466-469)
- Test: `tests/graph/test_summaries.py`, `tests/mcp/test_subsystem_summaries.py`

**Interfaces:**
- Consumes: `list_base_members` 5-кортеж + `get_updated_ats` (Task 2); `Settings.summary_rebuild_cap`.
- Produces:
  - `Member(node_id, path, content_hash, skeleton_hash, start_line)` (новое поле `skeleton_hash`).
  - `build_clusters` `source_hash` теперь из `skeleton_hash`.
  - `list_subsystem_clusters(repo, branch=None, depth=None, min_size=None, cap=None) -> {"branch", "deferred", "clusters":[{cluster_key, num_members, files, top_symbols, source_hash, stale}]}` — cap отбрасывает наименее приоритетные stale.
  - `index_subsystem_summary` — consistency-check на `skeleton_hash`.

- [ ] **Step 1: Тесты `summaries` (падающие)**

В `tests/graph/test_summaries.py` обновить `_m` (строки 6-7) и добавить тест freshness:

```python
def _m(node_id, path, h="h", line=1, sk=None):
    return Member(node_id=node_id, path=path, content_hash=h, start_line=line,
                  skeleton_hash=sk if sk is not None else h)


def test_build_clusters_source_hash_uses_skeleton_not_body():
    base = [_m("p/a.py#A", "p/a.py", h="body1", sk="sig1"),
            _m("p/b.py#B", "p/b.py", h="body2", sk="sig2")]
    [c0] = build_clusters(base, None, depth=1)
    # правка только тела (skeleton тот же) → тот же source_hash
    body = [_m("p/a.py#A", "p/a.py", h="BODYX", sk="sig1"),
            _m("p/b.py#B", "p/b.py", h="body2", sk="sig2")]
    [c1] = build_clusters(body, None, depth=1)
    assert c1.source_hash == c0.source_hash
    # смена сигнатуры (skeleton изменился) → другой source_hash
    sig = [_m("p/a.py#A", "p/a.py", h="body1", sk="SIGX"),
           _m("p/b.py#B", "p/b.py", h="body2", sk="sig2")]
    [c2] = build_clusters(sig, None, depth=1)
    assert c2.source_hash != c0.source_hash
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `.venv/bin/pytest tests/graph/test_summaries.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'skeleton_hash'`.

- [ ] **Step 3: Реализовать `summaries.py`**

`reviewer/graph/summaries.py` — `Member` (строки 14-19): добавить поле `skeleton_hash`:

```python
@dataclass
class Member:
    node_id: str          # "path#fqn"
    path: str
    content_hash: str
    skeleton_hash: str    # хэш структурного скелета — ключ свежести по структуре (PRI-165)
    start_line: int
```

`compute_source_hash` (строки 44-50) — обновить докстринг (логика без изменений):

```python
def compute_source_hash(items: list[tuple[str, str]]) -> str:
    """sha256 от sorted("node_id:skeleton_hash") — детерминированный ключ свежести.

    Меняется при изменении состава кластера или СТРУКТУРЫ его членов (сигнатуры/docstring),
    но НЕ при правке тела (PRI-165: вход — skeleton_hash, не content_hash). Сортировка пар
    делает ключ независимым от порядка членов."""
    joined = "\n".join(sorted(f"{nid}:{h}" for nid, h in items))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
```

`build_clusters` (строка 87): заменить `m.content_hash` на `m.skeleton_hash`:

```python
            source_hash=compute_source_hash([(m.node_id, m.skeleton_hash) for m in ms]),
```

- [ ] **Step 4: Запустить — `summaries` проходят**

Run: `.venv/bin/pytest tests/graph/test_summaries.py -q`
Expected: PASS.

- [ ] **Step 5: Добавить поле в `Settings`**

`reviewer/config/settings.py` — после строки 71 (`summary_cluster_depth`) добавить:

```python
    summary_rebuild_cap: int | None = None   # макс. кластеров на пересборку за проход
    # (PRI-165); None/0 = безлимит; env SUMMARY_REBUILD_CAP
```

- [ ] **Step 6: Тесты `service` (падающие)**

В `tests/mcp/test_subsystem_summaries.py`:
1. Во ВСЕХ моках `list_base_members.return_value` заменить 4-кортежи на 5-кортежи (добавить 5-й элемент — skeleton_hash). Пример для `test_list_subsystem_clusters_marks_stale`:

```python
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1"),
        ("reviewer/index/b.py", "B", "h2", 2, "sk2"),
    ]
```

2. В `test_list_subsystem_clusters_fresh_when_hash_matches` мок → 5-кортеж и эталон от `skeleton_hash`:

```python
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    c.graph = None
    from reviewer.graph.summaries import compute_source_hash
    sh = compute_source_hash([("reviewer/index/a.py#A", "sk1")])   # по skeleton_hash, не "h1"
    c.summary_store.get_source_hashes.return_value = {"reviewer/index": sh}
```

3. В `test_index_and_get_subsystem_summaries_roundtrip_via_store` мок → 5-кортежи, эталон `sh` от skeleton_hash (5-й элемент):

```python
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1"),
        ("reviewer/index/b.py", "B", "h2", 2, "sk2"),
    ]
    sh = compute_source_hash([("reviewer/index/a.py#A", "sk1"),
                              ("reviewer/index/b.py#B", "sk2")])
```

4. В `test_index_subsystem_summary_stale_hash_empties_members` мок → 5-кортеж:

```python
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
```

5. Добавить тесты cap + порядка:

```python
def test_list_subsystem_clusters_cap_defers_lowest_priority():
    from datetime import datetime
    c = MagicMock()
    # три кластера-одиночки (depth=2 даёт разные ключи), все stale
    c.store.list_base_members.return_value = [
        ("a/x/f.py", "F", "h", 1, "skf"),
        ("b/y/g.py", "G", "h", 1, "skg"),
        ("c/z/h.py", "H", "h", 1, "skh"),
    ]
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}          # все stale
    # a/x — без сводки (нет в updated); b/y старее c/z
    c.summary_store.get_updated_ats.return_value = {
        "b/y": datetime(2026, 1, 1), "c/z": datetime(2026, 6, 1)}
    svc = _svc(c)
    out = svc.list_subsystem_clusters("o/n", "dev", min_size=1, cap=2)
    keys = {cl["cluster_key"] for cl in out["clusters"]}
    assert out["deferred"] == 1
    assert keys == {"a/x", "b/y"}        # без сводки + старейший; c/z отложен


def test_list_subsystem_clusters_no_cap_returns_all():
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("a/x/f.py", "F", "h", 1, "skf"), ("b/y/g.py", "G", "h", 1, "skg")]
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}
    svc = _svc(c)
    out = svc.list_subsystem_clusters("o/n", "dev", min_size=1)   # cap=None
    assert out["deferred"] == 0
    assert len(out["clusters"]) == 2
    c.summary_store.get_updated_ats.assert_not_called()           # порядок не нужен без cap
```

- [ ] **Step 7: Запустить — `service` падают**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -q`
Expected: FAIL — `list_subsystem_clusters` ещё распаковывает 4-кортеж / нет `cap`/`deferred`.

- [ ] **Step 8: Реализовать `list_subsystem_clusters`**

Заменить `reviewer/mcp/service.py:419-447` на:

```python
    def list_subsystem_clusters(self, repo: str, branch: str | None = None,
                                depth: int | None = None, min_size: int | None = None,
                                cap: int | None = None) -> dict:
        """Кластеризовать base-граф по модулям → кластеры для /summarize-subsystems.
        cap (дефолт Settings.summary_rebuild_cap; None/0=безлимит) отбрасывает наименее
        приоритетные stale-кластеры (без сводки → старейшие updated_at первыми) и считает
        их в deferred (PRI-165)."""
        from reviewer.graph.summaries import Member, build_clusters
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"clusters": [], "note": rb}
        repo, resolved = rb
        raw = self.components.store.list_base_members(repo, resolved)
        if not raw:
            return {"clusters": [],
                    "note": "(base-индекс пуст — выполните /reviewer_sync-codebase)"}
        members = [Member(node_id=f"{p}#{s}", path=p, content_hash=h, start_line=sl,
                          skeleton_hash=sk)
                   for p, s, h, sl, sk in raw]
        graph = self.components.graph
        in_degree_fn = (
            (lambda ids: graph.in_degree(repo, ids, branch=resolved))
            if graph is not None else None)
        clusters = build_clusters(
            members, in_degree_fn,
            depth=depth or self.settings.summary_cluster_depth,
            min_size=min_size or 1)
        stored = self.components.summary_store.get_source_hashes(repo, resolved)
        stale = {c.key: (stored.get(c.key) != c.source_hash) for c in clusters}
        effective_cap = cap if cap is not None else self.settings.summary_rebuild_cap
        deferred_keys: set[str] = set()
        if effective_cap and effective_cap > 0:
            stale_cl = [c for c in clusters if stale[c.key]]
            if len(stale_cl) > effective_cap:
                updated = self.components.summary_store.get_updated_ats(repo, resolved)
                never = [c for c in stale_cl if c.key not in updated]      # без сводки — первыми
                aged = sorted((c for c in stale_cl if c.key in updated),
                              key=lambda c: updated[c.key])                # старейшие — раньше
                deferred_keys = {c.key for c in (never + aged)[effective_cap:]}
        return {"branch": resolved, "deferred": len(deferred_keys), "clusters": [
            {"cluster_key": c.key, "num_members": c.num_members, "files": c.files,
             "top_symbols": c.top_symbols, "source_hash": c.source_hash,
             "stale": stale[c.key]}
            for c in clusters if c.key not in deferred_keys]}
```

- [ ] **Step 9: Реализовать `index_subsystem_summary` (skeleton-консистентность)**

В `reviewer/mcp/service.py` заменить строки 466-469 (внутри `index_subsystem_summary`):

```python
        raw = self.components.store.list_base_members(repo, resolved)
        members = [(f"{p}#{s}", sk) for p, s, _h, _sl, sk in raw
                   if cluster_key_of(p, depth) == cluster_key]
        consistent = compute_source_hash(members) == source_hash
```

(было: `for p, s, h, _ in raw` и пара `(f"{p}#{s}", h)` — теперь 5-кортеж и `skeleton_hash`, синхронно с `build_clusters`.)

- [ ] **Step 10: Запустить — весь unit-suite зелёный**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py tests/graph/test_summaries.py -q`
Expected: PASS.
Run (полный unit-прогон — ничего не сломали): `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 11: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/graph/summaries.py reviewer/config/settings.py reviewer/mcp/service.py tests/graph/test_summaries.py tests/mcp/test_subsystem_summaries.py
git add reviewer/graph/summaries.py reviewer/config/settings.py reviewer/mcp/service.py tests/graph/test_summaries.py tests/mcp/test_subsystem_summaries.py
git commit -m "feat(mcp): freshness сводок по skeleton_hash + cap пересборок (PRI-165)"
```

- [ ] **Step 12: Integration-санити (нужен Postgres) — реальный путь снова согласован**

Run: `docker compose up -d && .venv/bin/pytest -m integration tests/index/test_summary_store.py -q`
Expected: PASS (закрывает «не запускать реальный MCP между Task 2 и Task 3» — теперь согласовано).

---

### Task 4: скилл `summarize-subsystems` — репорт `deferred` + выбор модели

**Files:**
- Modify: `plugin/skills/summarize-subsystems/SKILL.md` (шаги 2–4 + новый шаг выбора модели)
- Test: `tests/skills/` (существующий guard — должен остаться зелёным)

**Interfaces:**
- Consumes: `list_subsystem_clusters(...) -> {..., "deferred": int, "clusters": [...]}` (Task 3).
- Produces: инструкции скилла (репорт `deferred`; выбор дешёвой модели; диспатч summary-субагентов где харнесс умеет). Изменения только в прозе промпта; `<!-- include: ... -->`-маркеры сохраняются.

- [ ] **Step 1: Зафиксировать базовое состояние guard-теста**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (до правок).

- [ ] **Step 2: Переписать шаги Pipeline в `SKILL.md`**

Заменить блок «## Pipeline» (строки 21–48, до «## Grounding (hard rule)») на:

````markdown
## Pipeline

1. **Resolve repo/branch.**

<!-- include: _common/branch-selection.md -->

2. **List clusters.** Call `list_subsystem_clusters(repo, branch)`. Empty / `note` about an empty
   index → tell the user (in Russian) to run `/reviewer_sync-codebase` first, then stop. The response
   also carries `deferred` — the number of stale clusters the server held back this pass under the
   cost cap (env `SUMMARY_REBUILD_CAP`); the `clusters` it returns are already capped, so just process
   them and report `deferred` in step 4.

3. **Choose the summary model (only if any cluster is `stale == true`).** A subsystem summary is a
   coarse, high-level prior — a small/cheap model is appropriate, and reviewing on an expensive model
   burns tokens. Ask the user which model tier to use for writing summaries, defaulting to a cheap
   tier (e.g. Haiku/Sonnet/Fable). Remember the choice for this run. If nothing is stale, skip this
   step (nothing to generate).

4. **Summarize only STALE clusters.** For each cluster with `stale == true` (fresh ones are already
   up to date — skip them, this keeps the pass incremental and cheap):
   - Where your harness supports per-subagent model override, **dispatch a subagent on the chosen
     model** to read a few representative files (from `files` / `top_symbols`) and return
     `{title, summary}` (Russian, grounded — see Grounding below); the orchestrator then persists it.
     Where override is unavailable, write the summary inline on the session model and note this in the
     report. Either way:
     - `title` — one line: what this subsystem is.
     - `summary` — a compact paragraph: what it does, its key symbols (from `top_symbols`) and
       invariants. No `path:line` required; it is a high-level prior.
   - Persist: `index_subsystem_summary(repo, branch, cluster_key, title, summary, source_hash)` —
     pass back the cluster's own `source_hash` from step 2.

5. **Report (Russian).** How many clusters summarized vs skipped-as-fresh vs **deferred by the cap**
   (`deferred` from step 2 — never silently truncate). If summaries were written inline (no model
   override), say so.
````

(Существующая нумерация шага «4. Report» становится шагом 5; `<!-- include: _common/branch-selection.md -->` сохранён в шаге 1.)

- [ ] **Step 3: Запустить guard-тест**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (маркеры include не тронуты, сборка промпта валидна).

- [ ] **Step 4: Полный unit-прогон (регрессия)**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/summarize-subsystems/SKILL.md
git commit -m "feat(skills): summarize-subsystems — репорт deferred + выбор модели для сводок (PRI-165)"
```

---

## Self-Review (выполнено автором плана)

**1. Покрытие спеки:**
- Freshness по скелету → Task 1 (`symbol_skeleton_hash`) + Task 3 (`build_clusters` использует `skeleton_hash`). ✓
- skeleton-хэш на лету (без миграции) → Task 2 (`list_base_members` SELECT `text`). ✓
- Оба call-site синхронно → Task 3 Steps 8–9 (`list_subsystem_clusters` + `index_subsystem_summary`). ✓
- Cap server-side + порядок (без сводки → старейшие) + `deferred` → Task 3 Step 8 + тесты Step 6. ✓
- `updated_at` для порядка → Task 2 (`get_updated_ats`). ✓
- `Settings.summary_rebuild_cap` → Task 3 Step 5. ✓
- Выбор модели + диспатч + репорт `deferred` → Task 4. ✓
- Тесты (критерии приёмки 1–3): Task 1 (сигнатура vs тело vs позиция), Task 3 (body-only не ре-стейлит / cap / порядок / consistency). ✓
- Инвариант позиционной независимости → Task 1 `test_skeleton_hash_is_position_independent`. ✓
- Одноразовый шторм при апгрейде — поведенческий, гасится cap'ом; зафиксирован в спеке + репорте скилла (Task 4 шаг 5). ✓

**2. Плейсхолдеры:** нет — каждый шаг содержит полный код/команды.

**3. Согласованность типов:** `Member(node_id, path, content_hash, skeleton_hash, start_line)` — конструируется в `service` (Task 3 Step 8, kwargs) и `_m` (Task 3 Step 1, kwargs). `list_base_members` 5-кортеж `(path, fqn, content_hash, start_line, skeleton_hash)` — продюсер Task 2, потребители Task 3 (`for p,s,h,sl,sk`) и `index_subsystem_summary` (`for p,s,_h,_sl,sk`). `compute_source_hash` сигнатура неизменна. `get_updated_ats -> dict[str, datetime]`. Порядок зелёности per-task задокументирован (unit мокает границу store↔service).

## Execution Handoff
