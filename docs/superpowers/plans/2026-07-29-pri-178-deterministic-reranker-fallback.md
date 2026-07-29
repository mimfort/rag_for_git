# PRI-178 Deterministic Reranker Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать bounded fallback `search_codebase` детерминированным, сохранить минимальное graph-разнообразие и сообщать потребителю о реальной деградации reranker.

**Architecture:** `Retriever.search_base` будет получать graph neighbors через существующий ordered `GraphStore.expand_detailed`, восстанавливать этот порядок после `fetch_nodes` и разделять surviving chunks на hybrid/graph provenance. Чистый selector соберёт bounded fallback, а `ContextPack` получит типизированную причину деградации и безопасную trailing-note.

**Tech Stack:** Python 3.11–3.13, dataclasses, `typing.Literal`, pytest, Neo4j-backed `GraphStore` (без изменения schema), Ruff.

## Global Constraints

- Язык проекта — русский: новые комментарии, docstrings и диагностический текст писать по-русски.
- Line length — не более 100 символов; Ruff target — Python 3.11.
- Не добавлять зависимости, настройки `CodebaseLimits`, миграции или schema changes.
- Не менять `Retriever.retrieve`, ANN prefilter и успешный `rerank_scored -> select_by_cliff`.
- Не менять Voyage retry `6×22s`, timeout или circuit breaker.
- Unit-тесты не используют внешнюю сеть, Postgres, Neo4j или localhost; integration-тесты не входят в обязательный локальный прогон.
- `ContextPack(items=...)` должен остаться обратно совместимым.

---

## File Map

- `reviewer/retrieval/retriever.py` — provenance, ordered graph merge, чистый fallback selector, degraded metadata и rendering note.
- `tests/retrieval/test_search_base.py` — поведение selector и оба no-rerank пути на уровне `search_base`.
- `tests/retrieval/test_output_shaping.py` — точный контракт диагностической заметки `ContextPack.as_context`.
- `tests/retrieval/test_retriever_branch.py` — fake graph переводится на `expand_detailed`, сохраняется branch-routing regression.
- `docs/superpowers/specs/2026-07-29-pri-178-deterministic-reranker-fallback-design.md` — источник требований; кодовой правки не требует.

---

### Task 1: Детерминированный source-aware selector и ordered graph merge

**Files:**

- Modify: `reviewer/retrieval/retriever.py:11-159`
- Modify: `tests/retrieval/test_search_base.py:18-206`
- Modify: `tests/retrieval/test_retriever_branch.py:32-61`

**Interfaces:**

- Consumes: `GraphStore.expand_detailed(repo: str, node_ids: list[str], hops: int = 2, *, branch: str = "") -> list[dict]`, где dict содержит `id`, `rels`, `dist` и список упорядочен по `(dist, id)`.
- Produces: `_select_degraded_context(hybrid_items: list, graph_items: list, ceiling: int) -> list`.
- Produces: `search_base` передаёт в успешный reranker общий cleaned pool `hybrid_items + graph_items`; no-rerank пути используют `_select_degraded_context`.

- [ ] **Step 1: Перевести retrieval fakes на ordered graph API**

В `tests/retrieval/test_search_base.py` заменить `_FakeGraph`:

```python
class _FakeGraph:
    def __init__(self, related=(), raise_=False):
        self._related = list(related)
        self.expand_calls = []
        self._raise = raise_

    def expand_detailed(self, repo, node_ids, hops=2, *, branch=""):
        self.expand_calls.append({"repo": repo, "seeds": list(node_ids), "hops": hops,
                                  "branch": branch})
        if self._raise:
            raise RuntimeError("neo4j down")
        return list(self._related)
```

В существующих тестах передавать metadata:

```python
graph = _FakeGraph([{"id": "e.py#neighbor", "rels": ["CALLS"], "dist": 1}])
```

В `tests/retrieval/test_retriever_branch.py` оставить `expand` для `retrieve` и добавить
отдельный метод для `search_base`:

```python
def expand_detailed(self, repo, node_ids, hops=2, *, branch=""):
    self.branches.append(branch)
    return []
```

- [ ] **Step 2: Написать падающие unit-тесты чистого selector**

Добавить импорт и parameterized test в `tests/retrieval/test_search_base.py`:

```python
import pytest

from reviewer.retrieval.retriever import _select_degraded_context


@pytest.mark.parametrize(
    ("hybrid_ids", "graph_ids", "ceiling", "expected"),
    [
        (["h1", "h2", "h3"], ["g1", "g2"], 3, ["h1", "h2", "g1"]),
        (["h1"], ["g1", "g2"], 3, ["h1", "g1", "g2"]),
        (["h1", "h2"], ["g1"], 1, ["h1"]),
        ([], ["g1", "g2"], 1, ["g1"]),
        (["h1", "h2"], [], 2, ["h1", "h2"]),
        (["h1"], ["g1"], 0, []),
    ],
)
def test_select_degraded_context(hybrid_ids, graph_ids, ceiling, expected):
    hybrid = [_Hit(f"{node_id}.py#f") for node_id in hybrid_ids]
    graph = [_Hit(f"{node_id}.py#f") for node_id in graph_ids]

    selected = _select_degraded_context(hybrid, graph, ceiling)

    assert [item.path.removesuffix(".py") for item in selected] == expected
```

- [ ] **Step 3: Запустить selector-тест и подтвердить RED**

Run:

```bash
.venv/bin/pytest -q tests/retrieval/test_search_base.py::test_select_degraded_context
```

Expected: collection error `ImportError: cannot import name '_select_degraded_context'`.

- [ ] **Step 4: Реализовать минимальный чистый selector**

Добавить в `reviewer/retrieval/retriever.py` после `_dedupe_overlapping`:

```python
def _select_degraded_context(hybrid_items: list, graph_items: list, ceiling: int) -> list:
    """Bounded fallback: hybrid приоритетен, graph даёт минимальное разнообразие."""
    if ceiling <= 0:
        return []
    if not hybrid_items:
        return graph_items[:ceiling]
    if ceiling == 1:
        return hybrid_items[:1]
    if len(hybrid_items) < ceiling:
        free = ceiling - len(hybrid_items)
        return [*hybrid_items, *graph_items[:free]]
    if graph_items:
        return [*hybrid_items[:ceiling - 1], graph_items[0]]
    return hybrid_items[:ceiling]
```

- [ ] **Step 5: Запустить selector-тест и подтвердить GREEN**

Run:

```bash
.venv/bin/pytest -q tests/retrieval/test_search_base.py::test_select_degraded_context
```

Expected: `6 passed`.

- [ ] **Step 6: Написать падающие search_base-тесты ordering и provenance**

Добавить helper:

```python
def _meta(node_id, dist=1, rels=None):
    return {"id": node_id, "dist": dist, "rels": rels or ["CALLS"]}
```

Добавить тесты:

```python
def test_search_base_restores_graph_order_after_fetch_nodes():
    hits = [_Hit("h1.py#f")]
    hits[0].bm25_hit = True
    graph = _FakeGraph([_meta("g1.py#f", dist=1), _meta("g2.py#f", dist=2)])
    store = _FakeStore(
        hits,
        related=[_Hit("g2.py#f"), _Hit("g1.py#f")],
    )
    retriever = Retriever(store, graph, _FakeEmbedder(), reranker=None)

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=3))

    assert [item.node_id for item in pack.items] == [
        "h1.py#f", "g1.py#f", "g2.py#f",
    ]


def test_search_base_no_reranker_reserves_best_graph_item():
    hits = [_Hit(f"h{i}.py#f") for i in range(1, 4)]
    for hit in hits:
        hit.bm25_hit = True
    graph = _FakeGraph([_meta("g1.py#f"), _meta("g2.py#f", dist=2)])
    store = _FakeStore(hits, related=[_Hit("g2.py#f"), _Hit("g1.py#f")])
    retriever = Retriever(store, graph, _FakeEmbedder(), reranker=None)

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=3))

    assert [item.node_id for item in pack.items] == [
        "h1.py#f", "h2.py#f", "g1.py#f",
    ]


def test_search_base_reranker_failure_uses_same_graph_reservation():
    hits = [_Hit(f"h{i}.py#f") for i in range(1, 4)]
    for hit in hits:
        hit.bm25_hit = True
    graph = _FakeGraph([_meta("g1.py#f")])
    store = _FakeStore(hits, related=[_Hit("g1.py#f")])
    reranker = _FakeReranker(raise_=True)
    retriever = Retriever(store, graph, _FakeEmbedder(), reranker)

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=3))

    assert [item.node_id for item in pack.items] == [
        "h1.py#f", "h2.py#f", "g1.py#f",
    ]


def test_search_base_filtered_graph_item_does_not_reserve_slot():
    hits = [_Hit("h1.py#f"), _Hit("h2.py#f")]
    for hit in hits:
        hit.bm25_hit = True
    graph = _FakeGraph([_meta("tests/test_graph.py#t")])
    store = _FakeStore(hits, related=[_Hit("tests/test_graph.py#t")])
    retriever = Retriever(store, graph, _FakeEmbedder(), reranker=None)

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=2))

    assert [item.node_id for item in pack.items] == ["h1.py#f", "h2.py#f"]


def test_search_base_deduped_graph_item_does_not_reserve_slot():
    hits = [
        _Hit("a.py#Foo", start_line=1, end_line=50),
        _Hit("b.py#f"),
    ]
    for hit in hits:
        hit.bm25_hit = True
    graph = _FakeGraph([_meta("a.py#Foo.method")])
    store = _FakeStore(
        hits,
        related=[_Hit("a.py#Foo.method", start_line=10, end_line=20)],
    )
    retriever = Retriever(store, graph, _FakeEmbedder(), reranker=None)

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=2))

    assert [item.node_id for item in pack.items] == ["a.py#Foo", "b.py#f"]
```

- [ ] **Step 7: Запустить новые search_base-тесты и подтвердить RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/retrieval/test_search_base.py::test_search_base_restores_graph_order_after_fetch_nodes \
  tests/retrieval/test_search_base.py::test_search_base_no_reranker_reserves_best_graph_item \
  tests/retrieval/test_search_base.py::test_search_base_reranker_failure_uses_same_graph_reservation \
  tests/retrieval/test_search_base.py::test_search_base_filtered_graph_item_does_not_reserve_slot \
  tests/retrieval/test_search_base.py::test_search_base_deduped_graph_item_does_not_reserve_slot
```

Expected: failures because `search_base` still calls `expand` and slices merged items.

- [ ] **Step 8: Реализовать ordered graph merge и surviving provenance**

В `search_base` заменить graph block и подготовку `items`:

```python
hybrid_ids = {hit.node_id for hit in hits}
graph_only_ids: list[str] = []
merged: dict[str, object] = {hit.node_id: hit for hit in hits}
if self.graph is not None and hits:
    try:
        seeds = [hit.node_id for hit in hits[:ceiling]]
        expanded = self.graph.expand_detailed(
            repo, seeds, hops=hops, branch=branch)
        graph_ids = [row["id"] for row in expanded]
        fetched = self.store.fetch_nodes(
            repo, graph_ids, "__none__", [], base_ref=bref)
        fetched_by_id = {item.node_id: item for item in fetched}
        related = [fetched_by_id[node_id] for node_id in graph_ids
                   if node_id in fetched_by_id]
        for item in related:
            if item.node_id not in hybrid_ids:
                graph_only_ids.append(item.node_id)
            merged.setdefault(item.node_id, item)
    except Exception:
        log.warning("search_base: graph-expansion недоступен", exc_info=True)

items = list(merged.values())
if not include_tests:
    items = [item for item in items if not _is_test_path(item.path)]
items = _dedupe_overlapping(items)
graph_only_id_set = set(graph_only_ids)
hybrid_items = [item for item in items if item.node_id in hybrid_ids]
graph_items = [item for item in items if item.node_id in graph_only_id_set]
```

Перед успешным rerank собирать cleaned pool как `items = [*hybrid_items, *graph_items]`.
No-rerank часть Task 1 структурировать полностью (Task 2 позже добавит metadata):

```python
items = [*hybrid_items, *graph_items]
if len(items) <= lim.floor:
    selected = (
        _select_degraded_context(hybrid_items, graph_items, ceiling)
        if len(items) > ceiling
        else items
    )
    return ContextPack(items=selected, max_chars=self.max_context_chars)

if self.reranker is None:
    selected = _select_degraded_context(hybrid_items, graph_items, ceiling)
    return ContextPack(items=selected, max_chars=self.max_context_chars)

try:
    scored = self.reranker.rerank_scored(query, items)
except Exception:
    log.warning("search_base: rerank недоступен — deterministic fallback", exc_info=True)
    selected = _select_degraded_context(hybrid_items, graph_items, ceiling)
    return ContextPack(items=selected, max_chars=self.max_context_chars)
```

Успешные `select_by_cliff` и `ContextPack(tail_meta=...)` оставить без изменений.

- [ ] **Step 9: Запустить Task 1 regression**

Run:

```bash
.venv/bin/pytest -q tests/retrieval/test_search_base.py \
  tests/retrieval/test_retriever_branch.py
```

Expected: all passed.

- [ ] **Step 10: Проверить lint и закоммитить Task 1**

Run:

```bash
.venv/bin/ruff check reviewer/retrieval/retriever.py \
  tests/retrieval/test_search_base.py tests/retrieval/test_retriever_branch.py
git diff --check
```

Expected: both commands exit 0.

Commit:

```bash
git add reviewer/retrieval/retriever.py tests/retrieval/test_search_base.py \
  tests/retrieval/test_retriever_branch.py
git commit -m "fix(retrieval): сохранить graph-контекст без reranker"
```

---

### Task 2: Типизированная degraded-note в ContextPack

**Files:**

- Modify: `reviewer/retrieval/retriever.py:1-159`
- Modify: `tests/retrieval/test_output_shaping.py:1-85`
- Modify: `tests/retrieval/test_search_base.py:178-206`

**Interfaces:**

- Consumes: `_select_degraded_context(hybrid_items: list, graph_items: list, ceiling: int) -> list` из Task 1.
- Produces: `DegradedReason = Literal["reranker_unconfigured", "reranker_failed"]`.
- Produces: `ContextPack.degraded_reason: DegradedReason | None`.
- Preserves: `ContextPack(items=...)`, `tail_meta` и `as_context(line_numbers: bool = False) -> str`.

- [ ] **Step 1: Написать падающие output-shaping тесты**

В `tests/retrieval/test_output_shaping.py` добавить:

```python
@pytest.mark.parametrize(
    ("reason", "fragment"),
    [
        ("reranker_unconfigured", "reranker не настроен"),
        ("reranker_failed", "reranker недоступен"),
    ],
)
def test_as_context_appends_degraded_note(reason, fragment):
    out = ContextPack(items=[_node()], degraded_reason=reason).as_context()

    assert fragment in out
    assert "deterministic hybrid+graph fallback" in out
    assert "def f():" in out


def test_as_context_default_has_no_degraded_note():
    out = ContextPack(items=[_node()]).as_context()

    assert "fallback" not in out
```

- [ ] **Step 2: Запустить output-shaping тесты и подтвердить RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/retrieval/test_output_shaping.py::test_as_context_appends_degraded_note \
  tests/retrieval/test_output_shaping.py::test_as_context_default_has_no_degraded_note
```

Expected: `TypeError: ContextPack.__init__() got an unexpected keyword argument 'degraded_reason'`.

- [ ] **Step 3: Добавить тип и безопасный formatter**

В `reviewer/retrieval/retriever.py` добавить import и formatter:

```python
from typing import Literal

DegradedReason = Literal["reranker_unconfigured", "reranker_failed"]

_DEGRADED_NOTES: dict[DegradedReason, str] = {
    "reranker_unconfigured": (
        "— reranker не настроен: применён deterministic hybrid+graph fallback; "
        "качество ранжирования снижено."
    ),
    "reranker_failed": (
        "— reranker недоступен: применён deterministic hybrid+graph fallback; "
        "качество ранжирования снижено."
    ),
}


def _format_degraded_note(reason: DegradedReason | None) -> str | None:
    return _DEGRADED_NOTES.get(reason) if reason is not None else None
```

Расширить dataclass:

```python
@dataclass
class ContextPack:
    items: list
    max_chars: int = 0
    max_tokens: int = 0
    tail_meta: object = None
    degraded_reason: DegradedReason | None = None
```

В конце `as_context`, после cliff-note:

```python
degraded_note = _format_degraded_note(self.degraded_reason)
if degraded_note:
    text = f"{text}\n\n{degraded_note}" if text else degraded_note
```

- [ ] **Step 4: Запустить output-shaping тесты и подтвердить GREEN**

Run:

```bash
.venv/bin/pytest -q tests/retrieval/test_output_shaping.py
```

Expected: all passed.

- [ ] **Step 5: Написать падающие reason-semantics тесты search_base**

Добавить в `tests/retrieval/test_search_base.py`:

```python
def test_search_base_marks_unconfigured_reranker():
    hits = [_Hit(f"h{i}.py#f") for i in range(1, 4)]
    for hit in hits:
        hit.bm25_hit = True
    retriever = Retriever(
        _FakeStore(hits),
        _FakeGraph(),
        _FakeEmbedder(),
        reranker=None,
    )

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=2))

    assert pack.degraded_reason == "reranker_unconfigured"


def test_search_base_marks_failed_reranker():
    hits = [_Hit(f"h{i}.py#f") for i in range(1, 4)]
    for hit in hits:
        hit.bm25_hit = True
    retriever = Retriever(
        _FakeStore(hits),
        _FakeGraph(),
        _FakeEmbedder(),
        _FakeReranker(raise_=True),
    )

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=2))

    assert pack.degraded_reason == "reranker_failed"


def test_search_base_small_pool_is_not_marked_degraded():
    hits = [_Hit("h1.py#f"), _Hit("h2.py#f")]
    for hit in hits:
        hit.bm25_hit = True
    reranker = _FakeReranker()
    retriever = Retriever(_FakeStore(hits), _FakeGraph(), _FakeEmbedder(), reranker)

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=4, ceiling=1))

    assert pack.degraded_reason is None
    assert reranker.calls == []


def test_search_base_successful_rerank_is_not_marked_degraded():
    hits = [_Hit(f"h{i}.py#f") for i in range(1, 4)]
    for hit in hits:
        hit.bm25_hit = True
    retriever = Retriever(
        _FakeStore(hits),
        _FakeGraph(),
        _FakeEmbedder(),
        _FakeReranker(scores=[0.9, 0.8, 0.7]),
    )

    pack = retriever.search_base("a/x", "x", limits=_cb(floor=1, ceiling=3))

    assert pack.degraded_reason is None
```

- [ ] **Step 6: Запустить reason-тесты и подтвердить RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/retrieval/test_search_base.py::test_search_base_marks_unconfigured_reranker \
  tests/retrieval/test_search_base.py::test_search_base_marks_failed_reranker \
  tests/retrieval/test_search_base.py::test_search_base_small_pool_is_not_marked_degraded \
  tests/retrieval/test_search_base.py::test_search_base_successful_rerank_is_not_marked_degraded
```

Expected: first two tests fail because `search_base` does not set `degraded_reason`.

- [ ] **Step 7: Wire degraded reasons только в реальные fallback-пути**

Структурировать no-rerank часть `search_base` так:

```python
if len(items) <= lim.floor:
    selected = (
        _select_degraded_context(hybrid_items, graph_items, ceiling)
        if len(items) > ceiling
        else items
    )
    return ContextPack(items=selected, max_chars=self.max_context_chars)

if self.reranker is None:
    return ContextPack(
        items=_select_degraded_context(hybrid_items, graph_items, ceiling),
        max_chars=self.max_context_chars,
        degraded_reason="reranker_unconfigured",
    )

try:
    scored = self.reranker.rerank_scored(query, items)
except Exception:
    log.warning("search_base: rerank недоступен — deterministic fallback", exc_info=True)
    return ContextPack(
        items=_select_degraded_context(hybrid_items, graph_items, ceiling),
        max_chars=self.max_context_chars,
        degraded_reason="reranker_failed",
    )
```

Успешный return оставить:

```python
return ContextPack(
    items=kept,
    max_chars=self.max_context_chars,
    tail_meta=tail_meta,
)
```

- [ ] **Step 8: Запустить весь retrieval-набор**

Run:

```bash
.venv/bin/pytest -q tests/retrieval
```

Expected: all passed.

- [ ] **Step 9: Проверить MCP consumers**

Run:

```bash
.venv/bin/pytest -q \
  tests/mcp/test_service.py \
  tests/mcp/test_context_limits_wiring.py
```

Expected: all passed; `search_codebase` и `definition` продолжают рендерить
`ContextPack.as_context(line_numbers=True)`.

- [ ] **Step 10: Проверить lint и закоммитить Task 2**

Run:

```bash
.venv/bin/ruff check reviewer/retrieval/retriever.py \
  tests/retrieval/test_output_shaping.py tests/retrieval/test_search_base.py
git diff --check
```

Expected: both commands exit 0.

Commit:

```bash
git add reviewer/retrieval/retriever.py \
  tests/retrieval/test_output_shaping.py tests/retrieval/test_search_base.py
git commit -m "feat(retrieval): сообщать о fallback reranker"
```

---

### Task 3: Полная регрессия и завершение ветки

**Files:**

- Verify only: `reviewer/retrieval/retriever.py`
- Verify only: `tests/retrieval/test_search_base.py`
- Verify only: `tests/retrieval/test_output_shaping.py`
- Verify only: `tests/retrieval/test_retriever_branch.py`

**Interfaces:**

- Consumes: готовые `_select_degraded_context`, `DegradedReason`, `ContextPack.degraded_reason`.
- Produces: проверенную ветку без незакоммиченных code/test изменений.

- [ ] **Step 1: Запустить полный unit-набор**

Run:

```bash
.venv/bin/pytest -q
```

Expected: не меньше baseline `2643 passed, 1 skipped, 92 deselected`; новые тесты
увеличивают число passed, ошибок нет.

- [ ] **Step 2: Запустить полный Ruff**

Run:

```bash
.venv/bin/ruff check .
```

Expected: exit 0.

- [ ] **Step 3: Проверить diff и историю**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -5
```

Expected: `git diff --check` exit 0; нет незакоммиченных code/test изменений; история
содержит docs commit и два implementation commits PRI-178.

- [ ] **Step 4: Провести обязательный code review**

Использовать `superpowers:requesting-code-review`. Review должен проверить:

- соответствие spec и критериев PRI-178;
- отсутствие изменений `Retriever.retrieve`, retry и `CodebaseLimits`;
- детерминизм graph ordering;
- bounded invariant `len(items) <= ceiling`;
- отсутствие утечки exception/provider details в degraded-note.

Если review находит проблему, исправить её через TDD, повторить targeted tests и создать
отдельный conventional commit.

- [ ] **Step 5: Повторить verification после review fixes**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
git status --short
```

Expected: тесты и lint проходят; рабочее дерево чистое.

- [ ] **Step 6: Перейти к интеграции**

Использовать `superpowers:finishing-a-development-branch`, сравнить ветку с `origin/dev`
и предложить пользователю merge/PR/сохранение worktree. После создания PR использовать
`rag-reviewer:reviewer_finish-task`, чтобы добавить PR в PRI-178 и закрыть карточку только
с явного подтверждения пользователя.
