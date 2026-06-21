# PRI-129 — Ранжирование находок по центральности в графе. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** При равной severity находка в высокоцентральном символе (много входящих `CALLS`) идёт выше при сортировке и реже отсекается cap'ом.

**Architecture:** Граф даёт центральность символа (входящие `CALLS`) → отдельный чистый модуль маппит каждую находку `(file, line)` в охватывающий символ и проставляет числовое поле `Finding.centrality` → `assemble_review` использует его как **третичный** ключ сортировки `(severity, centrality, confidence)`. Проводка — в `publish_review` между dedup и assemble. Всё fail-soft: нет графа/совпадения → `centrality=0.0`, порядок вырождается в текущий.

**Tech Stack:** Python 3.11+, Neo4j (граф, `reviewer/graph/store.py`), pgvector-стор (диапазоны символов, `reviewer/index/store.py::Retrieved`), pytest (unit + `integration`-маркер для Neo4j), ruff.

## Global Constraints

- Язык кода: **русский** — комментарии, докстринги, сообщения (verbatim из CLAUDE.md).
- Коммиты: **Conventional Commits на русском**, **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- ruff: `line-length 100`, target `py311` (`.venv/bin/ruff check .`).
- Тесты: `pytest` по умолчанию **исключает** `integration` (`addopts = -m 'not integration'`). Маркер `@pytest.mark.integration` — тесты, требующие поднятого Neo4j.
- **Семантика degree:** входящие `CALLS` (не in+out). Рёбра `CALLS` создаются через `MERGE` → на пару (caller→callee) ровно одно ребро.
- `Finding.centrality` **НЕ** входит в `fingerprint()` (идемпотентность комментариев не меняется).
- Fail-soft: `graph is None` / стор недоступен / символ не пойман → `centrality=0.0`, ревью не падает.
- Ветка `feat/pri-129-centrality-ranking` уже создана; спека закоммичена (`ba21485`).

## File Structure

| Файл | Ответственность | Действие |
|---|---|---|
| `reviewer/graph/store.py` | Запрос центральности символа в Neo4j | Modify: + метод `in_degree` |
| `reviewer/vcs/base.py` | Модель `Finding` | Modify: + поле `centrality: float = 0.0` |
| `reviewer/agent/centrality.py` | Маппинг находок → символ → центральность (чистая функция) | Create |
| `reviewer/mcp/service.py` | `publish_review` — проводка центральности | Modify: вызов `annotate_centrality` между dedup и assemble |
| `reviewer/agent/assemble.py` | Сортировка/cap находок | Modify: третичный ключ `-f.centrality` |
| `tests/graph/test_in_degree.py` | Тест `in_degree` (Neo4j) | Create |
| `tests/agent/test_centrality.py` | Тест маппинга на фейках | Create |
| `tests/agent/test_assemble.py` | Тест tie-breaker сортировки | Modify: + тест |
| `tests/mcp/test_publish.py` | Тест проводки + фикс фейка | Modify |

---

### Task 1: `GraphStore.in_degree` — центральность по входящим CALLS

**Files:**
- Modify: `reviewer/graph/store.py` (после метода `callers`, ~строка 94)
- Test: `tests/graph/test_in_degree.py` (create)

**Interfaces:**
- Consumes: ничего нового (использует существующий `self._driver`).
- Produces: `GraphStore.in_degree(repo: str, node_ids: list[str], *, branch: str = "") -> dict[str, int]` — словарь `{node_id: число_входящих_CALLS}`; узлы без вызывающих в словарь **не попадают**.

- [ ] **Step 1: Написать падающий тест** (`tests/graph/test_in_degree.py`)

```python
import pytest
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore


@pytest.mark.integration
def test_in_degree_counts_incoming_calls():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema()
    g.clear("a/x", branch="main")
    g.upsert_nodes("a/x", ["m.py#hub", "m.py#c1", "m.py#c2", "m.py#leaf"], branch="main")
    g.upsert_edges("a/x", [("m.py#c1", "CALLS", "m.py#hub"),
                           ("m.py#c2", "CALLS", "m.py#hub")], branch="main")
    try:
        deg = g.in_degree("a/x", ["m.py#hub", "m.py#leaf"], branch="main")
        assert deg.get("m.py#hub") == 2
        assert "m.py#leaf" not in deg          # нет вызывающих → ключ отсутствует
        assert g.in_degree("a/x", [], branch="main") == {}
    finally:
        g.clear("a/x", branch="main")
        g.close()
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest -m integration tests/graph/test_in_degree.py -v`
Expected: FAIL — `AttributeError: 'GraphStore' object has no attribute 'in_degree'`

- [ ] **Step 3: Реализовать метод** (вставить в `reviewer/graph/store.py` сразу после `callers`, перед `find_symbol`)

```python
    def in_degree(self, repo: str, node_ids: list[str], *, branch: str = "") -> dict[str, int]:
        """Число входящих CALLS на каждый символ (центральность = сколько мест зависит).

        Узлы без вызывающих в результат не попадают → вызывающий трактует
        отсутствие ключа как 0.
        """
        if not node_ids:
            return {}
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid "
            "MATCH (c:Symbol {repo: $repo, branch: $branch})-[:CALLS]->"
            "(s:Symbol {repo: $repo, branch: $branch, id: sid}) "
            "RETURN sid AS id, count(c) AS deg",
            ids=list(node_ids), repo=repo, branch=branch)
        return {r["id"]: r["deg"] for r in records}
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest -m integration tests/graph/test_in_degree.py -v`
Expected: PASS (требует поднятого Neo4j: `docker compose up -d`)

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/graph/store.py tests/graph/test_in_degree.py
git add reviewer/graph/store.py tests/graph/test_in_degree.py
git commit -m "feat(graph): in_degree — центральность символа по входящим CALLS (PRI-129)"
```

---

### Task 2: Поле `Finding.centrality` + модуль `annotate_centrality`

**Files:**
- Modify: `reviewer/vcs/base.py:31-49` (dataclass `Finding`)
- Create: `reviewer/agent/centrality.py`
- Test: `tests/agent/test_centrality.py` (create)

**Interfaces:**
- Consumes: `GraphStore.in_degree(repo, node_ids, *, branch)` (Task 1); `store.fetch_nodes_at(repo, node_ids, ref) -> list[Retrieved]` (существует, `reviewer/index/store.py`); `Retrieved` несёт `node_id`, `path`, `start_line`, `end_line`.
- Produces:
  - `Finding.centrality: float = 0.0` — новое поле модели.
  - `annotate_centrality(findings, graph, store, *, repo, branch, changed_node_ids, overlay_ref) -> None` — мутирует `f.centrality` каждой находки на месте.

- [ ] **Step 1: Добавить поле в `Finding`** (`reviewer/vcs/base.py`, после `code_quote`, строка 44)

Вставить новую строку в dataclass `Finding` (последним полем, чтобы не сломать позиционные вызовы — все существующие поля сохраняют порядок):

```python
    code_quote: str | None = None   # дословная цитата строки (для fuzzy snap)
    centrality: float = 0.0         # центральность символа (входящие CALLS); tie-breaker сортировки (PRI-129)
```

`fingerprint()` НЕ трогаем — `centrality` в ключ не входит (строки 46-49 без изменений).

- [ ] **Step 2: Написать падающие тесты** (`tests/agent/test_centrality.py`)

```python
from reviewer.agent.centrality import annotate_centrality
from reviewer.vcs.base import Finding
from reviewer.index.store import Retrieved


def _ret(node_id, start, end):
    path, fqn = node_id.split("#", 1)
    return Retrieved(node_id, path, fqn, "function", start, end, "", 0.0)


class _Graph:
    """Фейк графа: degree = {node_id: int}."""
    def __init__(self, degree):
        self._d = degree

    def in_degree(self, repo, ids, *, branch=""):
        return {nid: self._d[nid] for nid in ids if nid in self._d}


class _Store:
    """Фейк стора: fetch_nodes_at игнорирует ref, отдаёт заданные узлы по id."""
    def __init__(self, nodes):
        self._nodes = nodes

    def fetch_nodes_at(self, repo, node_ids, ref):
        return [n for n in self._nodes if n.node_id in node_ids]


def _f(file="a.py", line=5, **kw):
    d = dict(category="correctness", severity="high", file=file, line=line,
             side="RIGHT", message="m", suggestion=None, confidence=0.9)
    d.update(kw)
    return Finding(**d)


def test_maps_finding_to_enclosing_symbol():
    f = _f(line=5)
    annotate_centrality(
        [f], _Graph({"a.py#foo": 3}), _Store([_ret("a.py#foo", 1, 10)]),
        repo="r", branch="dev", changed_node_ids=["a.py#foo"], overlay_ref="pr:1")
    assert f.centrality == 3.0


def test_picks_narrowest_symbol_on_nesting():
    f = _f(line=5)
    nodes = [_ret("a.py#Cls", 1, 100), _ret("a.py#Cls.m", 4, 6)]   # вложенный метод уже
    annotate_centrality(
        [f], _Graph({"a.py#Cls": 9, "a.py#Cls.m": 2}), _Store(nodes),
        repo="r", branch="dev",
        changed_node_ids=["a.py#Cls", "a.py#Cls.m"], overlay_ref="pr:1")
    assert f.centrality == 2.0   # выбран самый узкий диапазон (метод), не класс


def test_miss_leaves_zero():
    f = _f(line=999)             # вне диапазона символа
    annotate_centrality(
        [f], _Graph({"a.py#foo": 3}), _Store([_ret("a.py#foo", 1, 10)]),
        repo="r", branch="dev", changed_node_ids=["a.py#foo"], overlay_ref="pr:1")
    assert f.centrality == 0.0


def test_fail_soft_no_graph():
    f = _f(line=5)
    annotate_centrality(
        [f], None, _Store([_ret("a.py#foo", 1, 10)]),
        repo="r", branch="dev", changed_node_ids=["a.py#foo"], overlay_ref="pr:1")
    assert f.centrality == 0.0
```

- [ ] **Step 3: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/agent/test_centrality.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.agent.centrality'`

- [ ] **Step 4: Реализовать модуль** (`reviewer/agent/centrality.py`)

```python
"""Обогащение находок центральностью символа в графе кода (PRI-129).

Центральность символа = число входящих CALLS (сколько мест зависит от него).
Находка в высокоцентральном «хабе» при равной severity должна идти выше при
сортировке и реже отсекаться cap'ом — поэтому каждой находке проставляется
``centrality``, используемая как tie-breaker в ``assemble_review``.
"""
from __future__ import annotations


def annotate_centrality(findings, graph, store, *, repo, branch,
                        changed_node_ids, overlay_ref) -> None:
    """Проставить ``f.centrality`` каждой находке (мутация на месте).

    Маппинг: ``(file, line)`` находки → охватывающий символ изменённого файла
    (самый узкий диапазон при вложенности) → число входящих CALLS символа.
    Fail-soft: нет графа/стора/изменённых символов/совпадений → ``centrality``
    остаётся дефолтным 0.0, порядок сортировки не меняется.
    """
    if graph is None or store is None or not changed_node_ids or not findings:
        return
    # Символы изменённых файлов с диапазонами строк (head-версия из overlay).
    by_file: dict[str, list[tuple[int, int, str]]] = {}
    for n in store.fetch_nodes_at(repo, changed_node_ids, overlay_ref):
        by_file.setdefault(n.path, []).append((n.start_line, n.end_line, n.node_id))

    # Для каждой находки — охватывающий символ (минимальная ширина диапазона).
    finding_nid: list[tuple[object, str | None]] = []
    to_query: set[str] = set()
    for f in findings:
        nid = None
        if f.line is not None:
            spans = [(end - start, node_id)
                     for start, end, node_id in by_file.get(f.file, [])
                     if start <= f.line <= end]
            if spans:
                nid = min(spans)[1]   # минимальный (end-start) = самый узкий диапазон
                to_query.add(nid)
        finding_nid.append((f, nid))

    if not to_query:
        return
    deg = graph.in_degree(repo, list(to_query), branch=branch)
    for f, nid in finding_nid:
        if nid is not None:
            f.centrality = float(deg.get(nid, 0))
```

- [ ] **Step 5: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/agent/test_centrality.py -v`
Expected: PASS (4 теста)

- [ ] **Step 6: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/vcs/base.py reviewer/agent/centrality.py tests/agent/test_centrality.py
git add reviewer/vcs/base.py reviewer/agent/centrality.py tests/agent/test_centrality.py
git commit -m "feat(agent): обогащение находок центральностью графа (PRI-129)"
```

---

### Task 3: Центральность как третичный ключ сортировки в `assemble`

**Files:**
- Modify: `reviewer/agent/assemble.py:267-270`
- Test: `tests/agent/test_assemble.py` (add)

**Interfaces:**
- Consumes: `Finding.centrality` (Task 2).
- Produces: изменённый порядок `ranked` — `(severity, centrality, confidence)`. Внешний контракт `assemble_review` без изменений (та же сигнатура, тот же `AssembledReview`).

- [ ] **Step 1: Написать падающий тест** (добавить в конец `tests/agent/test_assemble.py`; хелпер `_f` уже принимает `**kw` → `centrality=` пробросится)

```python
def test_centrality_breaks_severity_tie_and_survives_cap():
    # Обе находки: severity=high, confidence=0.9, строка 2 (в диффе). Различает центральность.
    leaf = _f(message="leaf", centrality=0.0)
    hub = _f(message="hub", centrality=5.0)
    res = assemble_review(
        [leaf, hub],                      # подаём leaf первым — сортировка должна поднять hub
        patches={"a.py": PATCH},
        sources={"a.py": SOURCE},
        existing_fps=set(),
        max_comments=1,                   # cap=1 → выживает только верхняя по рангу
        suggestions_mode="off",
    )
    assert len(res.inline_comments) == 1
    assert "hub" in res.inline_comments[0].body     # высокоцентральная опубликована inline
    assert "leaf" in res.summary                    # менее центральная отсечена cap'ом → сводка
    assert res.capped == 1
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/agent/test_assemble.py::test_centrality_breaks_severity_tie_and_survives_cap -v`
Expected: FAIL — `assert "hub" in ...` (без центральности порядок = входной, inline получает `leaf`)

- [ ] **Step 3: Изменить ключ сортировки** (`reviewer/agent/assemble.py`, строки 265-270)

Было:
```python
    # Составной ранг: сначала severity (critical > high > medium > low),
    # при равном severity — большая уверенность идёт раньше.
    ranked = sorted(
        verified,
        key=lambda f: (-_SEVERITY_RANK.get(f.severity, 0), -f.confidence),
    )
```

Стало:
```python
    # Составной ранг: сначала severity (critical > high > medium > low),
    # при равном severity — большая центральность символа (хаб важнее, PRI-129),
    # при равной центральности — большая уверенность идёт раньше.
    ranked = sorted(
        verified,
        key=lambda f: (-_SEVERITY_RANK.get(f.severity, 0), -f.centrality, -f.confidence),
    )
```

- [ ] **Step 4: Запустить весь тест-файл assemble — новый проходит, 5 старых не сломались**

Run: `.venv/bin/pytest tests/agent/test_assemble.py -v`
Expected: PASS (старые тесты строят `Finding` без `centrality` → дефолт 0.0 → порядок не меняется)

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/agent/assemble.py tests/agent/test_assemble.py
git add reviewer/agent/assemble.py tests/agent/test_assemble.py
git commit -m "feat(agent): центральность как tie-breaker сортировки находок (PRI-129)"
```

---

### Task 4: Проводка центральности в `publish_review`

**Files:**
- Modify: `reviewer/mcp/service.py` (импорт + `publish_review`, между dedup ~строка 556 и assemble ~строка 568)
- Modify: `tests/mcp/test_publish.py` (`_components` фейк + новый тест)

**Interfaces:**
- Consumes: `annotate_centrality(...)` (Task 2); `self.components.graph` (граф); `self.components.retriever.store` (стор); `p.changed_node_ids`, `p.overlay_ref`, `p.branch` (атрибуты `PreparedReview`).
- Produces: побочный эффект — `deduped` находки получают `centrality` ДО `assemble_review`.

- [ ] **Step 1: Обновить фейк `_components` в тесте** (`tests/mcp/test_publish.py`, функция `_components`, после `c.graph.find_symbol.return_value = []`, ~строка 58)

Без этого `in_degree`/`fetch_nodes_at` вернут `MagicMock` и сломают существующие publish-тесты. Добавить:

```python
    c.graph.in_degree.return_value = {}
    c.retriever.store.fetch_nodes_at.return_value = []
```

- [ ] **Step 2: Написать падающий тест проводки** (добавить в `tests/mcp/test_publish.py` после `test_publish_gates_low_severity_and_grounds_line`)

```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_annotates_centrality_from_graph(_ov, _ch) -> None:
    from reviewer.index.store import Retrieved
    svc, vcs, _ = _make_mcp_service_with_publish()
    # Находка RAW на a.py:2 ложится в символ a.py#foo (диапазон 1..10); граф даёт degree=4.
    svc.components.retriever.store.fetch_nodes_at.return_value = [
        Retrieved("a.py#foo", "a.py", "foo", "function", 1, 10, "", 0.0)
    ]
    svc.components.graph.in_degree.return_value = {"a.py#foo": 4}
    svc.prepare_review("o/r", 7)
    svc.publish_review("o/r", 7, summary="s", findings=[RAW], dry_run=True)
    # Центральность была запрошена ровно для пойманного символа.
    svc.components.graph.in_degree.assert_called_once()
    assert svc.components.graph.in_degree.call_args.args[1] == ["a.py#foo"]
```

- [ ] **Step 3: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_publish.py::test_publish_annotates_centrality_from_graph -v`
Expected: FAIL — `in_degree` не вызывался (`assert_called_once` → `AssertionError`), т.к. проводки ещё нет.

- [ ] **Step 4: Добавить импорт** (`reviewer/mcp/service.py`, рядом с прочими импортами `reviewer.agent.*`)

```python
from reviewer.agent.centrality import annotate_centrality
```

- [ ] **Step 5: Вставить проводку** (`reviewer/mcp/service.py`, в `publish_review` сразу после `deduped = dedup_findings(kept)`)

Было:
```python
        # 2) Gate (категория/severity/confidence/пути) + dedup.
        kept = [f for f in parsed if p.policy.gate(f)]
        deduped = dedup_findings(kept)

        # 3) Существующие fingerprint'ы — для идемпотентности (fail-soft).
```

Стало:
```python
        # 2) Gate (категория/severity/confidence/пути) + dedup.
        kept = [f for f in parsed if p.policy.gate(f)]
        deduped = dedup_findings(kept)

        # 2b) Центральность символа (граф) → tie-breaker сортировки в assemble (PRI-129).
        # Fail-soft: нет графа/стора/совпадений → centrality 0.0, порядок не меняется.
        annotate_centrality(
            deduped,
            self.components.graph,
            getattr(self.components.retriever, "store", None),
            repo=repo,
            branch=p.branch,
            changed_node_ids=p.changed_node_ids,
            overlay_ref=p.overlay_ref,
        )

        # 3) Существующие fingerprint'ы — для идемпотентности (fail-soft).
```

- [ ] **Step 6: Запустить тест проводки + весь publish-файл**

Run: `.venv/bin/pytest tests/mcp/test_publish.py -v`
Expected: PASS (новый тест + 7 существующих — фейк обновлён в Step 1, fetch_nodes_at→[] → centrality 0.0)

- [ ] **Step 7: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/mcp/service.py tests/mcp/test_publish.py
git add reviewer/mcp/service.py tests/mcp/test_publish.py
git commit -m "feat(mcp): проводка центральности в publish_review (PRI-129)"
```

---

### Task 5: Финальная верификация

**Files:** нет изменений кода — только прогон.

- [ ] **Step 1: Весь unit-набор зелёный**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены дефолтным `-m 'not integration'`).

- [ ] **Step 2: Integration-тест графа (при поднятом Neo4j)**

Run: `docker compose up -d && .venv/bin/pytest -m integration tests/graph/test_in_degree.py -v`
Expected: PASS. Если Neo4j недоступен — зафиксировать в отчёте, не блокировать (тест помечен `integration`).

- [ ] **Step 3: Линт всего диффа**

Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: чисто по затронутым файлам (репозиторий-вайд чистоты ruff не гарантирует — проверяем только изменённые).

- [ ] **Step 4: Сверка с критерием приёмки**

Подтвердить вручную по тесту `test_centrality_breaks_severity_tie_and_survives_cap`: при равной severity находка с большей центральностью идёт выше и переживает cap. ✅ соответствует карточке PRI-129.

---

## Self-Review (выполнено автором плана)

**1. Покрытие спеки:**
- Секция 1 (метрика `in_degree`) → Task 1. ✅
- Секция 2 (маппинг finding→символ + проводка) → Task 2 (маппинг) + Task 4 (проводка). ✅
- Секция 3 (поле `Finding.centrality`, ключ сортировки, fail-soft, тесты) → Task 2 (поле) + Task 3 (сортировка) + fail-soft в Task 2. ✅
- Границы (gate не трогаем, без флагов) → соблюдено, отдельных задач не требует. ✅

**2. Плейсхолдеры:** нет TBD/TODO; весь код приведён дословно. ✅

**3. Согласованность типов:** `in_degree(repo, node_ids, *, branch) -> dict[str,int]` одинаково в Task 1 (определение), Task 2 (фейк `_Graph.in_degree`), Task 4 (вызов). `annotate_centrality(findings, graph, store, *, repo, branch, changed_node_ids, overlay_ref)` одинаково в Task 2 (определение/тесты) и Task 4 (вызов). `Finding.centrality: float` — определено Task 2, читается Task 3, ставится Task 2/используется Task 4. `Retrieved` поля (`node_id/path/start_line/end_line`) — из существующего `reviewer/index/store.py`. ✅
