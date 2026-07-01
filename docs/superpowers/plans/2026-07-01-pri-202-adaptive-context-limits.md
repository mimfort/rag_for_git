# PRI-202 — Адаптивные лимиты контекста (cliff-cut + ANN-префильтр) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать охват контекста в `search_codebase` адаптивным (отсечка по «обрыву» скоров реранкера), с ANN-префильтром пула, per-repo конфигом в `.review.yml` и рельсами для `search_tasks`.

**Architecture:** Чистая функция `select_by_cliff` режет проскоренный реранкером список по гибридному правилу (`ratio` ∧ `abs_floor`, зажато в `[floor, ceiling]`). `search_base` всегда реранкает (через новый `rerank_scored`), предварительно отбросив ANN-далёкий не-BM25 шум. Лимиты резолвятся server-side из `.review.yml` ветки (зеркало `_resolve_summary_depth`). PR-путь `retrieve()` не трогаем.

**Tech Stack:** Python 3.11–3.13, dataclasses, psycopg + pgvector (ParadeDB), Voyage rerank-2.5, pytest, ruff (line-length 100), FastMCP.

## Global Constraints

- Язык кода/докстрингов/нот — **русский** (стиль проекта).
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких Co-Authored-By/Claude).
- Линт: `.venv/bin/ruff check .` — line-length 100, target py311.
- Unit-тесты на фейках; реальные Postgres/Voyage — только `@pytest.mark.integration` (`pytest` по умолчанию исключает их: `addopts = -m 'not integration'`).
- **PR-путь `Retriever.retrieve()` и `tests/retrieval/test_retriever.py` НЕ менять** — обратная совместимость (AC 8).
- Новые поля `Retrieved.ann_distance`/`bm25_hit` — опциональны (дефолты `None`/`False`).
- Параметры лимитов читаются **только из `.review.yml`** (env-слоя нет); отсутствие ключа → дефолт-константа.
- Ветка работы: `feat/pri-202-adaptive-context-limits` (бриф+спека уже закоммичены).

---

### Task 1: Конфиг — `ContextLimits` + парсинг в `ReviewPolicy`

**Files:**
- Create: `reviewer/policy/context_limits.py`
- Modify: `reviewer/policy/policy.py` (поле + `from_yaml`/`load`/`from_settings`)
- Test: `tests/policy/test_context_limits.py` (новый), `tests/policy/test_policy.py` (дополнить)

**Interfaces:**
- Produces:
  - `CodebaseLimits(floor=4, ceiling=15, ratio=0.5, abs_floor=0.3, candidate_pool=30, ann_distance_max=0.65)` — frozen dataclass.
  - `TasksLimits(floor=3, ceiling=8)`; `GraphLimits(hops=1, callers_topk=25)`.
  - `ContextLimits(search_codebase, search_tasks, graph)` + classmethod `from_review_yaml(data: dict) -> ContextLimits`.
  - `ReviewPolicy.context_limits: ContextLimits` (default-конструируется).

- [ ] **Step 1: Write the failing test**

`tests/policy/test_context_limits.py`:
```python
from reviewer.policy.context_limits import ContextLimits, CodebaseLimits


def test_defaults_when_no_block():
    cl = ContextLimits.from_review_yaml({})
    assert cl.search_codebase == CodebaseLimits()
    assert cl.search_codebase.candidate_pool == 30
    assert cl.search_tasks.ceiling == 8
    assert cl.graph.hops == 1 and cl.graph.callers_topk == 25


def test_partial_block_keeps_other_defaults():
    cl = ContextLimits.from_review_yaml(
        {"context_limits": {"search_codebase": {"ceiling": 30, "ratio": 0.4}}})
    assert cl.search_codebase.ceiling == 30
    assert cl.search_codebase.ratio == 0.4
    assert cl.search_codebase.floor == 4          # дефолт сохранён
    assert cl.search_codebase.abs_floor == 0.3
    assert cl.search_tasks.ceiling == 8           # подсекции не заданы → дефолт


def test_subsections_search_tasks_and_graph():
    cl = ContextLimits.from_review_yaml(
        {"context_limits": {"search_tasks": {"ceiling": 12}, "graph": {"hops": 2}}})
    assert cl.search_tasks.ceiling == 12
    assert cl.graph.hops == 2
    assert cl.graph.callers_topk == 25
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/policy/test_context_limits.py -q`
Expected: FAIL — `ModuleNotFoundError: reviewer.policy.context_limits`.

- [ ] **Step 3: Write minimal implementation**

`reviewer/policy/context_limits.py`:
```python
"""Лимиты контекста retrieval-тулов (PRI-202). Per-repo, читаются из .review.yml.
Env-слоя нет: отсутствие ключа → дефолт-константа из этого модуля."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CodebaseLimits:
    floor: int = 4               # минимум чанков всегда (даже при обрыве на 1-м)
    ceiling: int = 15            # потолок (токены/Voyage)
    ratio: float = 0.5           # брать пока score >= ratio*top
    abs_floor: float = 0.3       # и score >= abs_floor по абсолюту
    candidate_pool: int = 30     # верхний предел кандидатов до реранка
    ann_distance_max: float = 0.65  # ANN-префильтр: отбросить не-BM25 с cosine-дистанцией > порога


@dataclass(frozen=True)
class TasksLimits:
    floor: int = 3
    ceiling: int = 8


@dataclass(frozen=True)
class GraphLimits:
    hops: int = 1
    callers_topk: int = 25


@dataclass(frozen=True)
class ContextLimits:
    search_codebase: CodebaseLimits = field(default_factory=CodebaseLimits)
    search_tasks: TasksLimits = field(default_factory=TasksLimits)
    graph: GraphLimits = field(default_factory=GraphLimits)

    @classmethod
    def from_review_yaml(cls, data: dict | None) -> "ContextLimits":
        """Собрать лимиты из распарсенного .review.yml. Заданные ключи поверх дефолтов."""
        block = (data or {}).get("context_limits") or {}
        cb = block.get("search_codebase") or {}
        st = block.get("search_tasks") or {}
        gr = block.get("graph") or {}
        return cls(
            search_codebase=CodebaseLimits(
                floor=int(cb.get("floor", CodebaseLimits.floor)),
                ceiling=int(cb.get("ceiling", CodebaseLimits.ceiling)),
                ratio=float(cb.get("ratio", CodebaseLimits.ratio)),
                abs_floor=float(cb.get("abs_floor", CodebaseLimits.abs_floor)),
                candidate_pool=int(cb.get("candidate_pool", CodebaseLimits.candidate_pool)),
                ann_distance_max=float(
                    cb.get("ann_distance_max", CodebaseLimits.ann_distance_max)),
            ),
            search_tasks=TasksLimits(
                floor=int(st.get("floor", TasksLimits.floor)),
                ceiling=int(st.get("ceiling", TasksLimits.ceiling)),
            ),
            graph=GraphLimits(
                hops=int(gr.get("hops", GraphLimits.hops)),
                callers_topk=int(gr.get("callers_topk", GraphLimits.callers_topk)),
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/policy/test_context_limits.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Wire into `ReviewPolicy`**

В `reviewer/policy/policy.py` добавь импорт вверху:
```python
from reviewer.policy.context_limits import ContextLimits
```
В dataclass `ReviewPolicy` добавь поле (рядом с `summary_cluster_depth_overrides`):
```python
    context_limits: ContextLimits = field(default_factory=ContextLimits)  # PRI-202, только из .review.yml
```
В `from_yaml` (внутри `return cls(...)`, последним аргументом) добавь:
```python
            context_limits=ContextLimits.from_review_yaml(data),
```
В `load` перед `return policy` добавь:
```python
        if "context_limits" in data:
            policy.context_limits = ContextLimits.from_review_yaml(data)
```
`from_settings` НЕ меняем — поле остаётся дефолтным (константы, env не читаем).

- [ ] **Step 6: Add policy-integration test**

В `tests/policy/test_policy.py` допиши:
```python
def test_context_limits_from_yaml_overrides_defaults():
    p = ReviewPolicy.from_yaml("context_limits:\n  search_codebase:\n    ceiling: 25\n")
    assert p.context_limits.search_codebase.ceiling == 25
    assert p.context_limits.search_codebase.floor == 4


def test_context_limits_default_when_absent():
    p = ReviewPolicy.from_yaml("max_comments: 10\n")
    assert p.context_limits.search_codebase.candidate_pool == 30


def test_load_applies_context_limits_over_env_defaults():
    s = Settings(_env_file=None)
    p = ReviewPolicy.load(s, "context_limits:\n  graph:\n    hops: 2\n")
    assert p.context_limits.graph.hops == 2
```

- [ ] **Step 7: Run policy tests + lint**

Run: `.venv/bin/pytest tests/policy/ -q && .venv/bin/ruff check reviewer/policy/`
Expected: PASS, no lint errors.

- [ ] **Step 8: Commit**

```bash
git add reviewer/policy/context_limits.py reviewer/policy/policy.py \
        tests/policy/test_context_limits.py tests/policy/test_policy.py
git commit -m "feat(policy): context_limits из .review.yml (PRI-202)"
```

---

### Task 2: Чистый cliff-селектор — `select_by_cliff` + `TailMeta`

**Files:**
- Create: `reviewer/retrieval/cliff.py`
- Test: `tests/retrieval/test_cliff.py` (новый)

**Interfaces:**
- Produces:
  - `@dataclass TailMeta(kept_n, total_n, top_score, cut_score, drop_score, beyond_relevant, groups)` где `groups: list[tuple[str, int, float]]` = `(prefix, count, top_score)`.
  - `select_by_cliff(scored, *, floor_n, ceiling_n, ratio, abs_floor) -> tuple[list, TailMeta]`; `scored: list[tuple[item, float]]` отсортирован по score убыванием. `item` имеет `.path`.
  - `format_tail_note(meta: TailMeta) -> str | None` — строка-заметка или `None`, если `beyond_relevant == 0`.

- [ ] **Step 1: Write the failing test**

`tests/retrieval/test_cliff.py`:
```python
from reviewer.retrieval.cliff import select_by_cliff, format_tail_note


class _It:
    def __init__(self, path):
        self.path = path

    def __repr__(self):
        return f"It({self.path})"


def _scored(pairs):
    return [(_It(p), s) for p, s in pairs]


def test_relative_cliff_cuts_below_ratio_but_floor_lifts_minimum():
    scored = _scored([("a/1.py", 0.91), ("a/2.py", 0.34), ("a/3.py", 0.12), ("a/4.py", 0.05)])
    kept, meta = select_by_cliff(scored, floor_n=2, ceiling_n=15, ratio=0.5, abs_floor=0.3)
    # 0.34 < 0.91*0.5 → обрыв после 1-го, но floor_n=2 поднимает до 2
    assert [it.path for it in kept] == ["a/1.py", "a/2.py"]
    assert meta.top_score == 0.91 and meta.cut_score == 0.34


def test_long_high_run_capped_by_ceiling():
    scored = _scored([(f"a/{i}.py", 0.9 - i * 0.01) for i in range(20)])
    kept, meta = select_by_cliff(scored, floor_n=4, ceiling_n=10, ratio=0.5, abs_floor=0.3)
    assert len(kept) == 10
    assert meta.beyond_relevant > 0          # хвост ≥ abs_floor существует


def test_abs_floor_cuts_noise_when_top_is_low():
    scored = _scored([("a/1.py", 0.41), ("a/2.py", 0.38), ("a/3.py", 0.20)])
    kept, _ = select_by_cliff(scored, floor_n=1, ceiling_n=15, ratio=0.5, abs_floor=0.3)
    # 0.20 < abs_floor 0.3 → отсечён, хотя 0.20 >= 0.41*0.5=0.205? нет (0.20<0.205) — оба правила режут
    assert [it.path for it in kept] == ["a/1.py", "a/2.py"]


def test_tail_meta_groups_by_path_prefix_and_note_built():
    scored = _scored([
        ("reviewer/retrieval/x.py", 0.88), ("reviewer/retrieval/y.py", 0.71),
        ("reviewer/retrieval/z.py", 0.69), ("reviewer/index/a.py", 0.55),
        ("tests/t.py", 0.20),
    ])
    kept, meta = select_by_cliff(scored, floor_n=1, ceiling_n=2, ratio=0.5, abs_floor=0.3)
    assert len(kept) == 2
    assert meta.beyond_relevant == 2          # z.py(0.69)+a.py(0.55) ≥ abs_floor; tests(0.20) нет
    prefixes = {g[0] for g in meta.groups}
    assert "reviewer" in prefixes
    note = format_tail_note(meta)
    assert note and "ceiling" in note


def test_empty_and_single():
    assert select_by_cliff([], floor_n=4, ceiling_n=15, ratio=0.5, abs_floor=0.3)[0] == []
    kept, meta = select_by_cliff(_scored([("a/1.py", 0.5)]), floor_n=4, ceiling_n=15,
                                 ratio=0.5, abs_floor=0.3)
    assert len(kept) == 1 and format_tail_note(meta) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/retrieval/test_cliff.py -q`
Expected: FAIL — `ModuleNotFoundError: reviewer.retrieval.cliff`.

- [ ] **Step 3: Write minimal implementation**

`reviewer/retrieval/cliff.py`:
```python
"""Адаптивная отсечка контекста по «обрыву» скоров реранкера (PRI-202).

Чистая, без БД и сети. На вход — список (item, score), отсортированный по score
убыванием; на выход — оставленные items + метаданные хвоста для ленивой заметки.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


def _prefix(path: str) -> str:
    """Первый сегмент пути — грубая «подсистема»."""
    return path.replace("\\", "/").split("/", 1)[0]


@dataclass
class TailMeta:
    kept_n: int = 0
    total_n: int = 0
    top_score: float | None = None
    cut_score: float | None = None        # скор последнего взятого
    drop_score: float | None = None       # скор первого отброшенного
    beyond_relevant: int = 0              # за обрезом со score >= abs_floor
    groups: list[tuple[str, int, float]] = field(default_factory=list)  # (prefix, count, top)


def select_by_cliff(scored, *, floor_n, ceiling_n, ratio, abs_floor):
    """Отсечь хвост по гибридному правилу (ratio ∧ abs_floor) в рельсах [floor, ceiling].

    floor побеждает оба правила (минимум всегда); ceiling — жёсткий максимум.
    Возвращает (kept_items, TailMeta).
    """
    if not scored:
        return [], TailMeta()
    ceiling_n = max(ceiling_n, floor_n)        # ceiling приоритетнее, но не ниже floor
    top = scored[0][1]
    kept: list = []
    for i, (item, score) in enumerate(scored):
        if i < floor_n:
            kept.append((item, score))
            continue
        if len(kept) >= ceiling_n:
            break
        if score >= top * ratio and score >= abs_floor:
            kept.append((item, score))
        else:
            break
    tail = scored[len(kept):]
    return [it for it, _ in kept], _build_tail_meta(kept, tail, top, abs_floor, len(scored))


def _build_tail_meta(kept, tail, top, abs_floor, total):
    relevant = [(it, s) for it, s in tail if s >= abs_floor]
    grouped: dict[str, list[float]] = defaultdict(list)
    for it, s in relevant:
        grouped[_prefix(it.path)].append(s)
    groups = sorted(
        ((p, len(ss), max(ss)) for p, ss in grouped.items()),
        key=lambda g: g[2], reverse=True)
    return TailMeta(
        kept_n=len(kept), total_n=total, top_score=top,
        cut_score=kept[-1][1] if kept else None,
        drop_score=tail[0][1] if tail else None,
        beyond_relevant=len(relevant), groups=groups[:3])


def format_tail_note(meta: TailMeta) -> str | None:
    """Ленивая заметка о высокоскоровом хвосте за обрезом. None, если хвост нерелевантен."""
    if meta.beyond_relevant <= 0:
        return None
    grp = ", ".join(f"{p} ({top:.2f})" for p, _cnt, top in meta.groups)
    drop = f", обрыв на {meta.drop_score:.2f}" if meta.drop_score is not None else ""
    return (f"— контекст обрезан по cliff: {meta.kept_n} из {meta.total_n} "
            f"(скор {meta.top_score:.2f}→{meta.cut_score:.2f}{drop}). За обрезом ещё "
            f"{meta.beyond_relevant} релевантных: {grp}. Перевызови с большим ceiling, "
            f"чтобы включить.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/retrieval/test_cliff.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check reviewer/retrieval/cliff.py tests/retrieval/test_cliff.py
git add reviewer/retrieval/cliff.py tests/retrieval/test_cliff.py
git commit -m "feat(retrieval): чистый cliff-селектор select_by_cliff + TailMeta (PRI-202)"
```

---

### Task 3: Реранкер возвращает скоры — `rerank_scored`

**Files:**
- Modify: `reviewer/index/reranker.py`
- Test: `tests/index/test_reranker.py` (новый или дополнить, если есть)

**Interfaces:**
- Consumes: `VoyageReranker(client=...)` с методом клиента `.rerank(query, docs, model, top_k)` → `resp.results` (каждый `res` имеет `.index`, `.relevance_score`).
- Produces: `VoyageReranker.rerank_scored(query, items) -> list[tuple[item, float]]` (по убыванию score). `rerank(query, items, top_k)` сохраняет старую сигнатуру/семантику (только items).

- [ ] **Step 1: Write the failing test**

`tests/index/test_reranker.py`:
```python
from reviewer.index.reranker import VoyageReranker


class _Res:
    def __init__(self, index, score):
        self.index = index
        self.relevance_score = score


class _Resp:
    def __init__(self, results):
        self.results = results


class _FakeClient:
    def __init__(self, results):
        self._results = results
        self.calls = []

    def rerank(self, query, docs, model, top_k):
        self.calls.append({"query": query, "n": len(docs), "top_k": top_k})
        return _Resp(self._results)


class _It:
    def __init__(self, text):
        self.text = text


def test_rerank_scored_returns_item_score_pairs_in_order():
    items = [_It("a"), _It("b"), _It("c")]
    client = _FakeClient([_Res(2, 0.9), _Res(0, 0.4)])     # реранкер вернул c, a
    rr = VoyageReranker(client=client)
    out = rr.rerank_scored("q", items)
    assert [(it.text, sc) for it, sc in out] == [("c", 0.9), ("a", 0.4)]
    assert client.calls[0]["top_k"] == 3                    # реранкаем весь пул


def test_rerank_keeps_items_only_signature():
    items = [_It("a"), _It("b")]
    client = _FakeClient([_Res(1, 0.9), _Res(0, 0.4)])
    rr = VoyageReranker(client=client)
    out = rr.rerank("q", items, top_k=2)
    assert [it.text for it in out] == ["b", "a"]             # без скоров


def test_rerank_scored_empty():
    rr = VoyageReranker(client=_FakeClient([]))
    assert rr.rerank_scored("q", []) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/index/test_reranker.py -q`
Expected: FAIL — `AttributeError: 'VoyageReranker' object has no attribute 'rerank_scored'`.

- [ ] **Step 3: Write minimal implementation**

Замени тело `VoyageReranker` (после `__init__`) в `reviewer/index/reranker.py`:
```python
    def rerank_scored(self, query: str, items: list) -> list:
        """Реранк всего пула с сохранением скоров: list[(item, relevance_score)] по убыванию."""
        if not items:
            return []
        docs = [it.text for it in items]
        resp = with_voyage_retry(
            lambda: self._client.rerank(query, docs, model=self.model, top_k=len(docs)))
        return [(items[res.index], float(res.relevance_score)) for res in resp.results]

    def rerank(self, query: str, items: list, top_k: int) -> list:
        if not items:
            return []
        docs = [it.text for it in items]
        resp = with_voyage_retry(
            lambda: self._client.rerank(query, docs, model=self.model, top_k=top_k))
        return [items[res.index] for res in resp.results]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/index/test_reranker.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check reviewer/index/reranker.py tests/index/test_reranker.py
git add reviewer/index/reranker.py tests/index/test_reranker.py
git commit -m "feat(index): VoyageReranker.rerank_scored — скоры реранкера для cliff (PRI-202)"
```

---

### Task 4: Store отдаёт ANN-дистанцию и BM25-флаг

**Files:**
- Modify: `reviewer/index/store.py` (`Retrieved` + `hybrid_search`)
- Test: `tests/index/test_reranker.py` нет — отдельно `tests/index/test_store_ann_fields.py` (новый, unit на дефолты) + интеграционный в `tests/index/test_store_hybrid.py`

**Interfaces:**
- Produces: `Retrieved` + поля `ann_distance: float | None = None`, `bm25_hit: bool = False`. `hybrid_search` заполняет их: `ann_distance` — cosine-дистанция из ann-CTE (`None`, если чанк не в ANN top-cand), `bm25_hit` — был ли чанк в bm25-CTE.

- [ ] **Step 1: Write the failing unit test (дефолты dataclass)**

`tests/index/test_store_ann_fields.py`:
```python
from reviewer.index.store import Retrieved


def test_retrieved_new_fields_default():
    r = Retrieved(node_id="a.py#f", path="a.py", symbol_fqn="f", kind="function",
                  start_line=1, end_line=2, text="x", score=0.1)
    assert r.ann_distance is None
    assert r.bm25_hit is False


def test_retrieved_accepts_ann_fields():
    r = Retrieved(node_id="a.py#f", path="a.py", symbol_fqn="f", kind="function",
                  start_line=1, end_line=2, text="x", score=0.1,
                  ann_distance=0.42, bm25_hit=True)
    assert r.ann_distance == 0.42 and r.bm25_hit is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/index/test_store_ann_fields.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'ann_distance'`.

- [ ] **Step 3: Add fields to `Retrieved`**

В `reviewer/index/store.py` в dataclass `Retrieved` добавь два поля **после** `score`:
```python
    score: float
    ann_distance: float | None = None   # cosine-дистанция ANN (PRI-202); None — не в ANN top-cand
    bm25_hit: bool = False              # был ли чанк лексическим (BM25) совпадением
```

- [ ] **Step 4: Run unit test to verify it passes**

Run: `.venv/bin/pytest tests/index/test_store_ann_fields.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Modify `hybrid_search` SQL to surface the fields**

В `reviewer/index/store.py::hybrid_search` замени `ann`-CTE, финальный `SELECT` и сборку `Retrieved`:

`ann`-CTE — добавь дистанцию:
```python
        ann AS (
            SELECT id, (embedding <=> %(vec)s) AS dist,
                   RANK() OVER (ORDER BY embedding <=> %(vec)s) AS rank
            FROM chunks
            WHERE {where}
            ORDER BY embedding <=> %(vec)s LIMIT %(cand)s
        ),
```
Финальный `SELECT` (LEFT JOIN к ann/bm25; cardinality не раздувает SUM — в ann/bm25 ≤1 строки на id):
```python
        SELECT c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text,
               SUM(r.s) AS score,
               MIN(a.dist) AS ann_dist,
               bool_or(b.id IS NOT NULL) AS bm25_hit
        FROM rrf r JOIN chunks c USING (id)
        LEFT JOIN ann a ON a.id = c.id
        LEFT JOIN bm25 b ON b.id = c.id
        GROUP BY c.id, c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text
        ORDER BY score DESC LIMIT %(k)s
```
Сборка результата:
```python
        return [Retrieved(node_id=f"{p}#{f}", path=p, symbol_fqn=f, kind=k,
                          start_line=sl, end_line=el, text=t, score=float(sc),
                          ann_distance=(float(ad) if ad is not None else None),
                          bm25_hit=bool(bh))
                for (p, f, k, sl, el, t, sc, ad, bh) in rows]
```

- [ ] **Step 6: Add integration test for the SQL**

В `tests/index/test_store_hybrid.py` допиши (следуй стилю существующих integration-тестов файла — фикстуры/`@pytest.mark.integration`):
```python
import pytest


@pytest.mark.integration
def test_hybrid_search_surfaces_ann_distance_and_bm25_hit(store_with_two_chunks):
    # store_with_two_chunks — фикстура файла: индекс с известными чанками.
    store, repo = store_with_two_chunks
    hits = store.hybrid_search(repo, query_text="login token verify",
                               query_embedding=[0.0] * 1024, overlay_ref="__none__",
                               changed_paths=[], top_k=10, candidates=10, base_ref="base")
    assert hits
    for h in hits:
        assert (h.ann_distance is None) or isinstance(h.ann_distance, float)
        assert isinstance(h.bm25_hit, bool)
    # хотя бы один лексически совпавший чанк помечен bm25_hit
    assert any(h.bm25_hit for h in hits)
```
> Если в файле нет готовой фикстуры с известными чанками — переиспользуй ту, что уже строит индекс в существующих integration-тестах файла (имя возьми из `tests/index/test_store_hybrid.py`); не вводи новую инфраструктуру.

- [ ] **Step 7: Run unit tests (integration требует ParadeDB)**

Run: `.venv/bin/pytest tests/index/test_store_ann_fields.py -q && .venv/bin/ruff check reviewer/index/store.py`
Expected: PASS, no lint errors. (Integration — позже: `docker compose up -d && .venv/bin/pytest tests/index/test_store_hybrid.py -m integration -q`.)

- [ ] **Step 8: Commit**

```bash
git add reviewer/index/store.py tests/index/test_store_ann_fields.py tests/index/test_store_hybrid.py
git commit -m "feat(index): hybrid_search отдаёт ann_distance и bm25_hit (PRI-202)"
```

---

### Task 5: `search_base` — ANN-префильтр + always-rerank-scored + cliff + заметка

**Files:**
- Modify: `reviewer/retrieval/retriever.py` (`search_base`, `ContextPack`)
- Test: `tests/retrieval/test_search_base.py` (переписать фейки/тесты под новое поведение)

**Interfaces:**
- Consumes: `select_by_cliff`, `format_tail_note`, `TailMeta` (Task 2); `reranker.rerank_scored` (Task 3); `Retrieved.ann_distance`/`bm25_hit` (Task 4); `CodebaseLimits` (Task 1).
- Produces:
  - `ContextPack` + поле `tail_meta: TailMeta | None = None`; `as_context()` дописывает `format_tail_note`.
  - `Retriever.search_base(repo, query, *, limits=None, hops=1, ceiling_override=None, branch="", include_tests=False) -> ContextPack`. `limits: CodebaseLimits | None` (None → дефолт). Старый `top_k`-параметр удалён; потолок задаёт `ceiling_override or limits.ceiling`.

- [ ] **Step 1: Rewrite the test fakes + tests**

Замени `_FakeReranker` и тесты в `tests/retrieval/test_search_base.py` (фейк теперь со скорами; вызовы `search_base` без `top_k`):
```python
from reviewer.policy.context_limits import CodebaseLimits


class _FakeReranker:
    """Возвращает (item, score) по заранее заданным скорам в порядке входа."""
    def __init__(self, scores=None, raise_=False):
        self._scores = scores
        self.calls = []
        self._raise = raise_

    def rerank_scored(self, query, items):
        self.calls.append({"n": len(items)})
        if self._raise:
            raise RuntimeError("voyage down")
        items = list(items)
        scores = self._scores or [1.0 - i * 0.01 for i in range(len(items))]
        paired = list(zip(items, scores[:len(items)]))
        return sorted(paired, key=lambda p: p[1], reverse=True)


def _cb(**kw):
    return CodebaseLimits(**{**dict(floor=1, ceiling=15, ratio=0.5,
                                    abs_floor=0.3, candidate_pool=30, ann_distance_max=0.65), **kw})


def test_search_base_reranks_always_and_applies_cliff():
    hits = [_Hit("a.py#f1", score=0.9), _Hit("b.py#f2"), _Hit("c.py#f3")]
    for h in hits:                       # bm25-хиты → префильтр их не трогает
        h.bm25_hit = True
        h.ann_distance = 0.1
    store, graph = _FakeStore(hits), _FakeGraph()
    reranker = _FakeReranker(scores=[0.91, 0.34, 0.12])   # обрыв после 1-го
    r = Retriever(store, graph, _FakeEmbedder(), reranker, max_context_chars=8000)
    pack = r.search_base("a/x", "x", limits=_cb(floor=2))
    assert reranker.calls                # реранк вызван всегда
    assert [it.node_id for it in pack.items] == ["a.py#f1", "b.py#f2"]  # floor=2


def test_search_base_ann_prefilter_drops_far_non_bm25():
    keep = _Hit("a.py#keep", score=0.9); keep.bm25_hit = False; keep.ann_distance = 0.2
    bm = _Hit("b.py#bm", score=0.5); bm.bm25_hit = True; bm.ann_distance = 0.95  # плохой вектор, но лексика
    drop = _Hit("c.py#drop", score=0.4); drop.bm25_hit = False; drop.ann_distance = 0.95
    store = _FakeStore([keep, bm, drop])
    reranker = _FakeReranker(scores=[0.9, 0.8])
    r = Retriever(store, _FakeGraph(), _FakeEmbedder(), reranker)
    pack = r.search_base("a/x", "x", limits=_cb(floor=1, ann_distance_max=0.65))
    ids = {it.node_id for it in pack.items}
    assert "c.py#drop" not in ids        # далёкий не-BM25 отброшен ДО реранка
    assert reranker.calls[0]["n"] == 2   # реранкнули только 2, не 3


def test_search_base_no_reranker_returns_rrf_order():
    store = _FakeStore([_Hit("a.py#f1"), _Hit("b.py#f2")])
    r = Retriever(store, graph=None, embedder=_FakeEmbedder(), reranker=None)
    pack = r.search_base("a/x", "x", limits=_cb(ceiling=5))
    assert [it.node_id for it in pack.items] == ["a.py#f1", "b.py#f2"]


def test_search_base_reranker_failure_falls_back_to_rrf():
    hits = [_Hit("a.py#f1"), _Hit("b.py#f2"), _Hit("c.py#f3"), _Hit("d.py#f4")]
    for h in hits:
        h.bm25_hit = True
    store = _FakeStore(hits)
    r = Retriever(store, _FakeGraph(), _FakeEmbedder(), _FakeReranker(raise_=True))
    pack = r.search_base("a/x", "x", limits=_cb(ceiling=2))
    assert len(pack.items) == 2          # RRF-порядок, срез по ceiling
    assert pack.tail_meta is None        # заметка не пишется при фолбэке


def test_search_base_seeds_graph_with_configured_hops():
    hits = [_Hit("a.py#f1")]; hits[0].bm25_hit = True
    graph = _FakeGraph({"e.py#n"})
    store = _FakeStore(hits, related=[_Hit("e.py#n")])
    r = Retriever(store, graph, _FakeEmbedder(), _FakeReranker())
    r.search_base("a/x", "x", limits=_cb(), hops=2)
    assert graph.expand_calls[0]["hops"] == 2
```
> Удали старые тесты `test_search_base_reranks_when_many_hits_and_graph_adds_nothing` и любые, вызывающие `search_base(..., top_k=...)` — поведение заменено. Тест `test_search_base_is_base_only_and_seeds_graph_from_hits` обнови: добавь `h.bm25_hit=True` хитам и замени `top_k=3` на `limits=_cb()`; `_FakeReranker` теперь без `top_k` в `.calls`.
> `_Hit.__init__` дополни полями `self.ann_distance = None` и `self.bm25_hit = False` (чтобы префильтр читал их).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/retrieval/test_search_base.py -q`
Expected: FAIL — `TypeError`/`AttributeError` (search_base ещё старой сигнатуры).

- [ ] **Step 3: Add `tail_meta` to `ContextPack`**

В `reviewer/retrieval/retriever.py` вверху добавь импорт:
```python
from reviewer.retrieval.cliff import format_tail_note, select_by_cliff
```
В dataclass `ContextPack` добавь поле и допиши `as_context`:
```python
@dataclass
class ContextPack:
    items: list
    max_chars: int = 0
    max_tokens: int = 0
    tail_meta: object = None        # TailMeta | None (PRI-202); ленивая заметка о хвосте
```
В конце `as_context`, перед `return text`, вставь (после блока усечения):
```python
        note = format_tail_note(self.tail_meta) if self.tail_meta is not None else None
        if note:
            text = f"{text}\n\n{note}" if text else note
        return text
```
(Замени финальный `return text` на этот блок.)

- [ ] **Step 4: Rewrite `search_base`**

Замени метод `Retriever.search_base` целиком:
```python
    def search_base(self, repo, query, *, limits=None, hops=1, ceiling_override=None,
                    branch="", include_tests=False) -> ContextPack:
        """Гибрид-поиск по base-индексу ветки без PR-сессии — для /solve-task (PRI-202).

        ANN-префильтр (BM25-aware) → always rerank_scored → cliff-отсечка. Граф и
        реранкер fail-soft (откат на RRF-порядок + срез по ceiling, без заметки).
        """
        from reviewer.policy.context_limits import CodebaseLimits
        lim = limits or CodebaseLimits()
        ceiling = ceiling_override or lim.ceiling
        bref = base_ref(branch)
        qvec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(
            repo, query_text=query, query_embedding=qvec,
            overlay_ref="__none__", changed_paths=[],
            top_k=lim.candidate_pool, candidates=lim.candidate_pool, base_ref=bref)
        # ANN-префильтр: оставляем лексические хиты и близкие по вектору; далёкий не-BM25 шум режем
        hits = [h for h in hits if getattr(h, "bm25_hit", False)
                or (getattr(h, "ann_distance", None) is not None
                    and h.ann_distance <= lim.ann_distance_max)]
        merged: dict[str, object] = {}
        for h in hits:
            merged.setdefault(h.node_id, h)
        if self.graph is not None and hits:
            try:
                seeds = [h.node_id for h in hits[:ceiling]]
                related_ids = self.graph.expand(repo, seeds, hops=hops, branch=branch)
                related = self.store.fetch_nodes(repo, list(related_ids), "__none__", [],
                                                 base_ref=bref)
                for it in related:
                    merged.setdefault(it.node_id, it)   # graph-items префильтр не трогает
            except Exception:
                log.warning("search_base: graph-expansion недоступен", exc_info=True)
        items = list(merged.values())
        if not include_tests:
            items = [it for it in items if not _is_test_path(it.path)]
        items = _dedupe_overlapping(items)
        # Fail-soft: нет реранкера/пусто/мелкий пул → RRF-порядок, срез по ceiling, без заметки
        if self.reranker is None or len(items) <= lim.floor:
            return ContextPack(items=items[:ceiling], max_chars=self.max_context_chars)
        try:
            scored = self.reranker.rerank_scored(query, items)
        except Exception:
            log.warning("search_base: rerank недоступен — RRF-порядок", exc_info=True)
            return ContextPack(items=items[:ceiling], max_chars=self.max_context_chars)
        kept, tail_meta = select_by_cliff(
            scored, floor_n=lim.floor, ceiling_n=ceiling,
            ratio=lim.ratio, abs_floor=lim.abs_floor)
        return ContextPack(items=kept, max_chars=self.max_context_chars, tail_meta=tail_meta)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/retrieval/test_search_base.py -q`
Expected: PASS (все тесты файла).

- [ ] **Step 6: Verify PR-path untouched**

Run: `.venv/bin/pytest tests/retrieval/ -q && .venv/bin/ruff check reviewer/retrieval/`
Expected: PASS — `test_retriever.py` (PR-путь) зелёный без изменений.

- [ ] **Step 7: Commit**

```bash
git add reviewer/retrieval/retriever.py tests/retrieval/test_search_base.py
git commit -m "feat(retrieval): search_base — ANN-префильтр + cliff-отсечка реранкера (PRI-202)"
```

---

### Task 6: `graph_format` — cap как параметр

**Files:**
- Modify: `reviewer/tools/graph_format.py`
- Test: `tests/tools/test_graph_format.py` (дополнить; имя файла сверь в `tests/tools/`)

**Interfaces:**
- Produces: `format_neighbors(..., cap: int = 25)` — `_CAP` заменён параметром (дефолт-константа `_DEFAULT_CAP = 25`).

- [ ] **Step 1: Write the failing test**

В `tests/tools/` найди тест-файл graph_format (`grep -rl format_neighbors tests/tools/`) и допиши:
```python
def test_format_neighbors_respects_cap_param():
    neighbors = [{"id": f"a.py#f{i}", "rel": "CALLS"} for i in range(10)]
    out = format_neighbors(neighbors, store=None, repo="a/x", branch="dev",
                           overlay_ref="__none__", changed_paths=[], empty_msg="—", cap=3)
    assert out.count("// ") == 3
    assert "…ещё 7" in out
```
> Сигнатуру вызова сверь с существующими тестами файла (порядок именованных аргументов).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/tools/test_graph_format.py -q -k cap_param`
Expected: FAIL — `TypeError: format_neighbors() got an unexpected keyword argument 'cap'`.

- [ ] **Step 3: Parametrize the cap**

В `reviewer/tools/graph_format.py`:
- Замени `_CAP = 25` на `_DEFAULT_CAP = 25`.
- В сигнатуру `format_neighbors` добавь `cap: int = _DEFAULT_CAP` (после `empty_msg`).
- Внутри замени `_CAP` на `cap`: `items = neighbors[:cap]` и `if total > cap:` / `f"(…ещё {total - cap}, усечено)"`.

- [ ] **Step 4: Run test + existing graph_format tests**

Run: `.venv/bin/pytest tests/tools/test_graph_format.py -q && .venv/bin/ruff check reviewer/tools/graph_format.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add reviewer/tools/graph_format.py tests/tools/test_graph_format.py
git commit -m "feat(tools): callers cap как параметр format_neighbors (PRI-202)"
```

---

### Task 7: MCP-обвязка — `_resolve_context_limits` + `search_codebase`/граф-тулы + top_k-override

**Files:**
- Modify: `reviewer/mcp/service.py` (`_resolve_context_limits`, `search_codebase`, `related_symbols`, `find_callers`)
- Modify: `reviewer/entrypoints/mcp_server.py` (сигнатура/докстринг `search_codebase`)
- Test: `tests/mcp/test_context_limits_wiring.py` (новый)

**Interfaces:**
- Consumes: `ReviewPolicy.load` → `.context_limits` (Task 1); `Retriever.search_base(..., limits=, hops=, ceiling_override=)` (Task 5); `format_neighbors(..., cap=)` (Task 6).
- Produces:
  - `MCPReviewService._resolve_context_limits(repo, branch) -> ContextLimits` (fail-soft → `ContextLimits()`).
  - `search_codebase(repo, query, top_k: int | None = None, branch=None, include_tests=False)` — `top_k` = override потолка; передаёт `limits`/`hops`/`ceiling_override` в `search_base`.

- [ ] **Step 1: Write the failing test**

`tests/mcp/test_context_limits_wiring.py` (следуй стилю `tests/mcp/` — изоляция Settings/моки; ниже скелет с фейками):
```python
from reviewer.policy.context_limits import ContextLimits, CodebaseLimits


class _FakeRetriever:
    def __init__(self):
        self.calls = []

    def search_base(self, repo, query, *, limits=None, hops=1, ceiling_override=None,
                    branch="", include_tests=False):
        self.calls.append({"limits": limits, "hops": hops, "ceiling_override": ceiling_override})

        class _P:
            def as_context(self_inner, line_numbers=False):
                return "ok"
        return _P()


def test_resolve_context_limits_failsoft_returns_defaults(mcp_service_no_vcs):
    # mcp_service_no_vcs — фикстура: MCPReviewService без доступного VCS (чтение .review.yml упадёт)
    svc = mcp_service_no_vcs
    cl = svc._resolve_context_limits("o/r", "dev")
    assert isinstance(cl, ContextLimits)
    assert cl.search_codebase.ceiling == 15        # дефолт-константа


def test_search_codebase_passes_limits_and_topk_override(mcp_service_with_fake_retriever):
    svc, retr = mcp_service_with_fake_retriever     # retr = _FakeRetriever
    svc.search_codebase("o/r", "q", top_k=40, branch="dev")
    assert retr.calls[0]["ceiling_override"] == 40
    assert isinstance(retr.calls[0]["limits"], CodebaseLimits)
    assert retr.calls[0]["hops"] == 1
```
> Если в `tests/mcp/conftest.py`/файлах нет подходящих фикстур — собери `MCPReviewService` так же, как соседние тесты `tests/mcp/` (моки `components`, `settings`, `_vcs_factory`). `_resolve_context_limits` должен ловить исключение чтения VCS и возвращать дефолт.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/mcp/test_context_limits_wiring.py -q`
Expected: FAIL — `AttributeError: ... has no attribute '_resolve_context_limits'`.

- [ ] **Step 3: Add `_resolve_context_limits` (зеркало `_resolve_summary_depth`)**

В `reviewer/mcp/service.py` рядом с `_resolve_summary_depth` добавь:
```python
    def _resolve_context_limits(self, repo: str, branch: str):
        """Лимиты контекста из .review.yml ветки (PRI-202). Fail-soft → дефолт-константы."""
        from reviewer.policy.context_limits import ContextLimits
        from reviewer.policy.policy import ReviewPolicy
        owner, name = repo.split("/", 1)
        vcs = None
        try:
            vcs = (self._vcs_factory(owner, name) if self._vcs_factory
                   else self._review_service._create_vcs_provider(owner, name))
            text = vcs.get_file_at_ref(".review.yml", branch)
            if not text:
                return ContextLimits()
            return ReviewPolicy.load(self.settings, text).context_limits
        except Exception:
            log.warning("_resolve_context_limits: fail-soft → дефолт-константы", exc_info=True)
            return ContextLimits()
        finally:
            if vcs is not None and self._vcs_factory is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning("_resolve_context_limits: не удалось закрыть VCS", exc_info=True)
```

- [ ] **Step 4: Rewire `search_codebase` (+ граф-тулы)**

Замени тело `search_codebase` в `reviewer/mcp/service.py`:
```python
    def search_codebase(self, repo: str, query: str, top_k: int | None = None,
                        branch: str | None = None,
                        include_tests: bool = False) -> str:
        """Гибрид-поиск по base-индексу (без PR-сессии) — для /solve-task (PRI-202).

        Охват адаптивен (cliff-отсечка реранкера). top_k — необязательный override
        потолка (ceiling) для этого вызова; None → потолок из .review.yml/дефолта.
        """
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        cl = self._resolve_context_limits(repo, resolved)
        try:
            pack = self.components.retriever.search_base(
                repo, query, limits=cl.search_codebase, hops=cl.graph.hops,
                ceiling_override=top_k, branch=resolved, include_tests=include_tests)
        except Exception:
            log.warning("search_codebase: сбой поиска", exc_info=True)
            return "(ничего не найдено)"
        return pack.as_context(line_numbers=True) or "(ничего не найдено)"
```
В `related_symbols` и `find_callers` (session-less) после резолва ветки добавь резолв лимитов и проброс cap в `format_neighbors`:
```python
        cl = self._resolve_context_limits(repo, resolved)
        # ... в вызове format_neighbors(...) добавь: cap=cl.graph.callers_topk
```
> Найди вызовы `format_neighbors(` в `related_symbols`/`find_callers` и добавь `cap=cl.graph.callers_topk`.

- [ ] **Step 5: Update FastMCP tool signature/docstring**

В `reviewer/entrypoints/mcp_server.py` в `@mcp.tool() def search_codebase(...)`:
- Сигнатуру `top_k: int = 10` → `top_k: int | None = None`.
- В докстринге добавь строку: `top_k — необязательный override потолка выдачи (ceiling); None → из .review.yml/дефолта. Охват адаптивен (cliff).`

- [ ] **Step 6: Run tests + lint**

Run: `.venv/bin/pytest tests/mcp/ -q && .venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_context_limits_wiring.py
git commit -m "feat(mcp): search_codebase/граф-тулы читают context_limits + top_k-override (PRI-202)"
```

---

### Task 8: `search_tasks` — рельсы + заметка о хвосте

**Files:**
- Modify: `reviewer/tasks/service.py` (`search_tasks`)
- Modify: `reviewer/entrypoints/mcp_server.py` (сигнатура `search_tasks`)
- Modify: `reviewer/mcp/service.py` (`search_tasks` проброс)
- Test: `tests/tasks/test_search_tasks_rails.py` (новый)

**Interfaces:**
- Produces: `TaskService.search_tasks(query, top_k=None, project=None)` — `top_k` = override потолка (None → дефолт-константа `TasksLimits.ceiling=8`); фетчит больше кандидатов, возвращает ≤ceiling + заметку «показано N из M», если M>ceiling.

- [ ] **Step 1: Write the failing test**

`tests/tasks/test_search_tasks_rails.py` (следуй стилю `tests/tasks/` с `_FakeStore`/`_FakeEmbedder`):
```python
from reviewer.tasks.service import TaskService


class _Hit:
    def __init__(self, key):
        self.key, self.title, self.status, self.score = key, key, "—", 0.02


class _Store:
    def __init__(self, n):
        self._hits = [_Hit(f"ID-{i}") for i in range(n)]
        self.calls = []

    def search(self, query, vec, top_k, candidates=50, project=None):
        self.calls.append({"top_k": top_k, "candidates": candidates})
        return self._hits[:top_k]


class _Emb:
    def embed_query(self, q):
        return [0.0] * 8


def _svc(n):
    return TaskService(store=_Store(n), graph=None, embedder=_Emb(), max_chars=8000)


def test_search_tasks_caps_at_ceiling_and_notes_tail():
    out = _svc(14).search_tasks("q")          # дефолт ceiling=8, found=14
    lines = [ln for ln in out.splitlines() if ln and ln[0].isdigit()]
    assert len(lines) == 8
    assert "показано 8 из 14" in out


def test_search_tasks_topk_override_raises_ceiling():
    out = _svc(14).search_tasks("q", top_k=12)
    lines = [ln for ln in out.splitlines() if ln and ln[0].isdigit()]
    assert len(lines) == 12


def test_search_tasks_no_tail_note_when_within_ceiling():
    out = _svc(3).search_tasks("q")
    assert "показано" not in out
```
> Сверь конструктор `TaskService` с реальным (`reviewer/tasks/service.py`) — имена параметров могут отличаться; используй фактические.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/tasks/test_search_tasks_rails.py -q`
Expected: FAIL — заметки нет / cap не применён.

- [ ] **Step 3: Apply rails in `search_tasks`**

Замени тело `TaskService.search_tasks` в `reviewer/tasks/service.py`:
```python
    def search_tasks(self, query: str, top_k: int | None = None,
                     project: str | None = None) -> str:
        """Похожие задачи (RRF, без реранкера) с рельсой ceiling (PRI-202).

        top_k — override потолка (None → дефолт-константа). Фетчим больше кандидатов,
        возвращаем ≤ceiling, при хвосте дописываем заметку. Пусто/сбой → нота.
        """
        from reviewer.policy.context_limits import TasksLimits
        ceiling = top_k or TasksLimits.ceiling
        try:
            vec = self._embedder.embed_query(query)
            hits = self._store.search(query, vec, top_k=max(ceiling * 3, 30), project=project)
        except Exception:
            log.warning("search_tasks: сбой поиска по запросу %r", query, exc_info=True)
            return "(task search unavailable)"
        if not hits:
            return "(no similar tasks found)"
        total = len(hits)
        shown = hits[:ceiling]
        lines = [f"{i}. {h.key} [{h.status or '—'}] {h.title} (score {h.score:.4f})"
                 for i, h in enumerate(shown, 1)]
        if total > ceiling:
            lines.append(f"— показано {ceiling} из {total} (рельса ceiling). "
                         f"Перевызови с большим ceiling для остальных.")
        return "\n".join(lines)
```
> `max(ceiling * 3, 30)` — фетч-окно, чтобы знать `total` за рельсой (стор сам капит `candidates=50`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/tasks/test_search_tasks_rails.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Update signatures (mcp service + FastMCP tool)**

- `reviewer/mcp/service.py::search_tasks`: сигнатуру `top_k: int = 5` → `top_k: int | None = None` (тело уже делегирует `task_service.search_tasks(query, top_k, project=project)`).
- `reviewer/entrypoints/mcp_server.py::search_tasks` (`@mcp.tool()`): `top_k: int = 5` → `top_k: int | None = None`; в докстринг добавь: `top_k — override потолка (ceiling); None → дефолт.`

- [ ] **Step 6: Run tasks + mcp tests + lint**

Run: `.venv/bin/pytest tests/tasks/ tests/mcp/ -q && .venv/bin/ruff check reviewer/tasks/service.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add reviewer/tasks/service.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py \
        tests/tasks/test_search_tasks_rails.py
git commit -m "feat(tasks): search_tasks рельсы ceiling + заметка о хвосте (PRI-202)"
```

---

### Task 9: SKILL.md — адаптивный brief-cap + ленивый перевызов

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md`
- Modify: `plugin/skills/ask/SKILL.md`
- Test: `tests/skills/` (прогнать guard-тесты; дополнить при необходимости)

**Interfaces:**
- Consumes: новое поведение тулов (заметка о хвосте, top_k-override) из Tasks 5/7/8.

- [ ] **Step 1: Inspect skills guard tests**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (зафиксировать зелёный базлайн перед правками).

- [ ] **Step 2: Update solve-task relevance filter (адаптивный brief-cap)**

В `plugin/skills/solve-task/SKILL.md`, секция **Step 4 → Relevance filter**: замени фиксированные капы
«≤5 files/symbols», «≤3 test files/symbols» на адаптивную формулировку (НЕ трогая include-маркеры `<!-- include: _common/... -->`):
```markdown
   **Relevance filter (adaptive — retrieval is already bounded server-side).** Server-side cliff
   (search_codebase) and rails (search_tasks) already cap retrieval adaptively per task. So DO NOT
   re-truncate to a fixed number and DO NOT pad artificially: include EVERY returned item that
   *directly informs* the implementation. The keep/drop judgment stays binary (directly-informs),
   and end each section with `(dropped N: reason)`.
   - **Order** candidates by result rank.
   - **No fixed ceilings.** Take exactly the directly-informing items the tools returned. Related
     tasks are bounded by the search rails; the brief lists those that directly inform.
   - ✅ INCLUDE / ❌ EXCLUDE — as before (a symbol you'll edit or mimic; a task whose PR shows a
     concrete pattern; a constraint that narrows the approach).
```
> Удали из брифа-скелета числовые «≤3/≤5/≤3» в строках `## Related work`, `## Relevant code`,
> `## Test exemplars` (оставь сами секции и `(dropped N: …)`).

- [ ] **Step 3: Add lazy re-call instruction (solve-task)**

В `plugin/skills/solve-task/SKILL.md`, секция **Step 3 (Gather context)**, в пункт про `search_codebase`
допиши:
```markdown
   - **Lazy expansion (no user prompt).** If a tool's output ends with a cliff/rails note reporting a
     high-scoring tail beyond the cut AND the task looks broad, you MAY re-call the tool once with a
     higher ceiling (pass `top_k=<bigger>`), then merge. Do this silently — never pause to ask the user.
```

- [ ] **Step 4: Mirror minimal change into ask SKILL.md**

В `plugin/skills/ask/SKILL.md` (где описан `search_codebase`-вызов) добавь тот же абзац **Lazy expansion**.
Если у ask нет brief-cap секции — меняй только lazy-expansion (ask не строит бриф).

- [ ] **Step 5: Run skills guard tests**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS. Если guard-тест проверял наличие строк «≤5»/«≤3» — обнови ожидание в тесте под адаптивную формулировку (не ослабляя проверку include-маркеров).

- [ ] **Step 6: Commit**

```bash
git add plugin/skills/solve-task/SKILL.md plugin/skills/ask/SKILL.md tests/skills/
git commit -m "feat(solve-task): адаптивный brief-cap + ленивый перевызов под cliff (PRI-202)"
```

---

### Task 10: Полный прогон + финальная проверка

**Files:** —

- [ ] **Step 1: Full unit suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены по умолчанию).

- [ ] **Step 2: Lint**

Run: `.venv/bin/ruff check .`
Expected: без новых ошибок в изменённых файлах (репозиторный baseline ruff может быть не идеален — см. memory `refactor-verification-gotchas`; не гнаться за repo-wide clean, проверь только свои файлы при необходимости: `.venv/bin/ruff check reviewer/retrieval reviewer/policy reviewer/index reviewer/mcp reviewer/tasks reviewer/tools reviewer/entrypoints`).

- [ ] **Step 3: Integration (если поднята инфра)**

Run: `docker compose up -d && .venv/bin/pytest -m integration -q`
Expected: PASS (в частности `test_hybrid_search_surfaces_ann_distance_and_bm25_hit`). Voyage free tier — прогон может троттлиться; повторить при rate-limit.

- [ ] **Step 4: Spec-coverage sanity**

Сверь чек-лист AC спеки (раздел «Тесты (AC 8-9)») с зелёными тестами; убедись, что PR-путь (`tests/retrieval/test_retriever.py`) не менялся.

---

## Self-Review

**Spec coverage:**
- AC1 (`context_limits` в `.review.yml`, дефолты) → Task 1.
- AC2 (тулы берут лимиты) → Tasks 5, 7, 8.
- AC3 (always rerank, скоры) → Tasks 3, 5.
- AC4 (мета/заметка) → Tasks 2 (TailMeta/note), 5 (codebase), 8 (tasks). Структурный конверт заменён прозо-заметкой (решение спеки).
- AC5 (`hops` из конфига) → Tasks 5, 7.
- AC6 (`callers_topk` из конфига) → Tasks 6, 7.
- AC7 (SKILL.md) → Task 9 (interrupt заменён ленивым перевызовом — решение brainstorming).
- AC8 (старые тесты зелёные, PR-путь цел) → Tasks 4/5 (опц. поля, retrieve() не трогаем), Task 10.
- AC9 (новые unit-тесты: парсинг/мета/by_category/hops+callers_topk) → Tasks 1, 2, 5, 6, 7, 8.
- ANN-префильтр → Tasks 4 (store), 5 (retriever).

**Placeholder scan:** код приведён во всех шагах; в Tasks 4/6/7/8 есть пометки «сверь фикстуру/сигнатуру с существующими тестами» — это намеренная привязка к реальным фикстурам файла, не заглушка логики.

**Type consistency:** `select_by_cliff(scored, *, floor_n, ceiling_n, ratio, abs_floor) → (list, TailMeta)`; `rerank_scored(query, items) → [(item, float)]`; `Retrieved.ann_distance: float|None`, `.bm25_hit: bool`; `search_base(..., limits: CodebaseLimits, hops, ceiling_override)`; `ContextLimits.search_codebase/.search_tasks/.graph`. Имена согласованы между задачами.
