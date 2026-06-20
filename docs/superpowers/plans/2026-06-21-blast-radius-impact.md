# Blast-radius (get_impact) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить MCP-тул `get_impact` и измерение blast-radius в `review-pr`, чтобы ловить кросс-файловые поломки контракта (смена сигнатуры функции ломает её вызывающих вне диффа).

**Architecture:** Чистый движок `reviewer/tools/impact.py` (сигнатурный гейт base-vs-overlay → callers вне диффа → обогащение) питает PR-сессионный тул `get_impact` (3 слоя: code_tools → mcp/service → mcp_server). В плагине новое whole-diff измерение читает `get_impact` и выдаёт находки `category=correctness`, якорённые на строку изменённой сигнатуры. Движок даёт факты, вердикт «сломано ли» — за LLM.

**Tech Stack:** Python 3.11+, langchain `StructuredTool`, FastMCP, ParadeDB (pgvector + pg_search), Neo4j, pytest.

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения тулов, текст находок. (Тело SKILL/reference-промптов — английское, но просит LLM отвечать на языке `policy.output_language`.)
- Каждый MCP-тул = **3 слоя**: `reviewer/tools/code_tools.py` (замыкание) → `reviewer/mcp/service.py` (`_invoke_tool`) → `reviewer/entrypoints/mcp_server.py` (`@mcp.tool()`).
- **node_id = `"path#fqn"`** — единый ключ RAG↔граф.
- Unit-тесты на фейках (`pytest -q`, integration исключены `addopts`); тесты, требующие ParadeDB/Neo4j, помечаются `@pytest.mark.integration`.
- Линт: `.venv/bin/ruff check .` (line-length 100, target py311).
- Коммиты: Conventional Commits на русском, **без self-attribution**.

---

### Task 1: `extract_signature` — выделение заголовка def/class

**Files:**
- Create: `reviewer/tools/impact.py`
- Test: `tests/tools/test_impact.py`

**Interfaces:**
- Produces: `extract_signature(node_text: str) -> str | None` — нормализованный заголовок (`def`/`async def`/`class` … до закрывающей `:` на верхнем уровне скобок), пробелы схлопнуты; `None`, если заголовок не найден.

- [ ] **Step 1: Write the failing tests**

```python
# tests/tools/test_impact.py
from reviewer.tools.impact import extract_signature


def test_extract_signature_single_line():
    assert extract_signature("def f(a, b):\n    return a") == "def f(a, b):"


def test_extract_signature_with_annotations():
    text = "def f(a: int, b: str = 'x') -> bool:\n    ..."
    assert extract_signature(text) == "def f(a: int, b: str = 'x') -> bool:"


def test_extract_signature_multiline():
    text = "def f(\n    a,\n    b,\n):\n    return a"
    assert extract_signature(text) == "def f( a, b, ):"


def test_extract_signature_async_and_decorator():
    text = "@cache\nasync def f(x):\n    return x"
    assert extract_signature(text) == "async def f(x):"


def test_extract_signature_class():
    assert extract_signature("class A(B, C):\n    pass") == "class A(B, C):"


def test_extract_signature_none_when_absent():
    assert extract_signature("x = 1\ny = 2") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/tools/test_impact.py -q`
Expected: FAIL with `ImportError: cannot import name 'extract_signature'`

- [ ] **Step 3: Write minimal implementation**

```python
# reviewer/tools/impact.py
"""Анализ радиуса поражения (blast-radius): изменённые сигнатуры PR → вызывающие вне диффа."""
from __future__ import annotations
import re

from reviewer.index.refs import base_ref

_DEF_RE = re.compile(r"^\s*(async\s+def|def|class)\s")
_WS_RE = re.compile(r"\s+")


def extract_signature(node_text: str) -> str | None:
    """Заголовок объявления (def/async def/class) из исходника символа.

    Сканирует до первой `:` на нулевой глубине скобок — корректно для
    многострочных сигнатур и аннотаций (`x: int` внутри скобок не считается
    концом заголовка). Декораторы и докстринги до `def`/`class` пропускаются.
    Возвращает строку с нормализованными пробелами или None, если заголовка нет.
    """
    lines = node_text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _DEF_RE.match(ln)), None)
    if start is None:
        return None
    rest = "\n".join(lines[start:])
    depth = 0
    end = None
    for j, ch in enumerate(rest):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            end = j
            break
    header = rest[: end + 1] if end is not None else rest
    return _WS_RE.sub(" ", header).strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/tools/test_impact.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add reviewer/tools/impact.py tests/tools/test_impact.py
git commit -m "feat(tools): extract_signature для blast-radius (PRI-126)"
```

---

### Task 2: `compute_impact` + `format_impact` — движок blast-radius

**Files:**
- Modify: `reviewer/tools/impact.py`
- Test: `tests/tools/test_impact.py`

**Interfaces:**
- Consumes: `extract_signature` (Task 1); `base_ref(branch) -> str` из `reviewer.index.refs` (даёт `"base:<branch>"`); `graph.callers(repo, node_ids, *, branch) -> set[str]`; `store.fetch_nodes_at(repo, node_ids, ref) -> list[Retrieved]` (Task 3); `store.fetch_nodes(repo, node_ids, overlay_ref, changed_paths, *, base_ref) -> list[Retrieved]` (существует). `Retrieved` имеет поля `node_id, path, start_line, text`.
- Produces:
  - `@dataclass CallerRef{node_id: str, path: str, line: int, snippet: str}`
  - `@dataclass ImpactItem{node_id: str, old_sig: str, new_sig: str, callers: list[CallerRef]}`
  - `compute_impact(graph, store, *, repo, branch, changed_node_ids, changed_paths, overlay_ref) -> list[ImpactItem]`
  - `format_impact(items: list[ImpactItem]) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/tools/test_impact.py  (добавить ниже)
from reviewer.tools.impact import (
    compute_impact, format_impact, ImpactItem, CallerRef,
)
from reviewer.index.store import Retrieved


def _ret(node_id, text):
    path, fqn = node_id.split("#", 1)
    return Retrieved(node_id, path, fqn, "function", 10, 20, text, 0.0)


class _Graph:
    def __init__(self, callers_map):
        self._c = callers_map

    def callers(self, repo, ids, *, branch=""):
        out = set()
        for nid in ids:
            out |= set(self._c.get(nid, []))
        return out


class _Store:
    """Фейк: by_ref = {ref: {node_id: text}}."""
    def __init__(self, by_ref):
        self._by_ref = by_ref

    def fetch_nodes_at(self, repo, node_ids, ref):
        m = self._by_ref.get(ref, {})
        return [_ret(nid, m[nid]) for nid in node_ids if nid in m]

    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths, *, base_ref="base"):
        m = self._by_ref.get(base_ref, {})
        return [_ret(nid, m[nid]) for nid in node_ids if nid in m]


def test_compute_impact_flags_external_callers_on_signature_change():
    graph = _Graph({"svc.py#f": ["a.py#g", "b.py#h", "svc.py#local"]})
    store = _Store({
        "pr:1": {"svc.py#f": "def f(a, b, c):\n    ..."},
        "base:dev": {
            "svc.py#f": "def f(a, b):\n    ...",
            "a.py#g": "def g():\n    f(1, 2)",
            "b.py#h": "def h():\n    f(1, 2)",
        },
    })
    items = compute_impact(graph, store, repo="r", branch="dev",
                           changed_node_ids=["svc.py#f"], changed_paths=["svc.py"],
                           overlay_ref="pr:1")
    assert len(items) == 1
    it = items[0]
    assert it.node_id == "svc.py#f"
    assert it.old_sig == "def f(a, b):"
    assert it.new_sig == "def f(a, b, c):"
    assert {c.path for c in it.callers} == {"a.py", "b.py"}  # svc.py#local в диффе → отфильтрован
    assert all(c.line == 10 for c in it.callers)


def test_compute_impact_gate_skips_body_only_change():
    graph = _Graph({"svc.py#f": ["a.py#g"]})
    store = _Store({
        "pr:1": {"svc.py#f": "def f(a, b):\n    return a + b"},
        "base:dev": {"svc.py#f": "def f(a, b):\n    return a - b"},
    })
    items = compute_impact(graph, store, repo="r", branch="dev",
                           changed_node_ids=["svc.py#f"], changed_paths=["svc.py"],
                           overlay_ref="pr:1")
    assert items == []


def test_compute_impact_skips_added_symbol():
    graph = _Graph({"svc.py#new": ["a.py#g"]})
    store = _Store({"pr:1": {"svc.py#new": "def new(a):\n    ..."}})  # нет base-версии
    items = compute_impact(graph, store, repo="r", branch="dev",
                           changed_node_ids=["svc.py#new"], changed_paths=["svc.py"],
                           overlay_ref="pr:1")
    assert items == []


def test_compute_impact_no_external_callers_skipped():
    graph = _Graph({"svc.py#f": ["svc.py#local"]})  # вызывающий в том же изменённом файле
    store = _Store({
        "pr:1": {"svc.py#f": "def f(a, b, c):\n    ..."},
        "base:dev": {"svc.py#f": "def f(a, b):\n    ..."},
    })
    items = compute_impact(graph, store, repo="r", branch="dev",
                           changed_node_ids=["svc.py#f"], changed_paths=["svc.py"],
                           overlay_ref="pr:1")
    assert items == []


def test_format_impact_renders_callers():
    items = [ImpactItem("svc.py#f", "def f(a):", "def f(a, b):",
                        [CallerRef("a.py#g", "a.py", 10, "def g():")])]
    out = format_impact(items)
    assert "svc.py#f" in out and "def f(a, b):" in out and "a.py:10" in out


def test_format_impact_empty():
    assert "не найдено" in format_impact([])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/tools/test_impact.py -q`
Expected: FAIL with `ImportError: cannot import name 'compute_impact'`

- [ ] **Step 3: Write minimal implementation**

```python
# reviewer/tools/impact.py  (добавить: импорт dataclass вверху и тело ниже extract_signature)
# в начало файла, к импортам:
from dataclasses import dataclass, field


@dataclass
class CallerRef:
    node_id: str
    path: str
    line: int
    snippet: str


@dataclass
class ImpactItem:
    node_id: str
    old_sig: str
    new_sig: str
    callers: list[CallerRef] = field(default_factory=list)


def compute_impact(graph, store, *, repo, branch, changed_node_ids,
                   changed_paths, overlay_ref):
    """Символы изменённых файлов с РЕАЛЬНО изменённой сигнатурой → их вызывающие вне диффа.

    Гейт: сигнатура символа в overlay (head) != сигнатура в base (до PR).
    Это отсекает чисто внутренние рефакторинги (тело поменяли, контракт нет).
    Вызывающие, чей файл входит в diff (changed_paths), отфильтрованы — их автор уже видит.
    Возвращает [] при отсутствии графа/стора/изменений.
    """
    if graph is None or store is None or not changed_node_ids:
        return []
    changed = set(changed_paths or [])
    new_by_id = {n.node_id: n for n in store.fetch_nodes_at(repo, changed_node_ids, overlay_ref)}
    old_by_id = {n.node_id: n for n in store.fetch_nodes_at(repo, changed_node_ids, base_ref(branch))}

    items: list[ImpactItem] = []
    for nid in changed_node_ids:
        old, new = old_by_id.get(nid), new_by_id.get(nid)
        if old is None or new is None:
            continue  # добавленный/удалённый символ — нет пары для сравнения
        old_sig, new_sig = extract_signature(old.text), extract_signature(new.text)
        if not old_sig or not new_sig or old_sig == new_sig:
            continue  # ГЕЙТ: сигнатура не менялась
        caller_ids = sorted(graph.callers(repo, [nid], branch=branch))
        external = [cid for cid in caller_ids if cid.split("#", 1)[0] not in changed]
        if not external:
            continue
        nodes = {n.node_id: n for n in
                 store.fetch_nodes(repo, external, overlay_ref, changed_paths,
                                   base_ref=base_ref(branch))}
        callers: list[CallerRef] = []
        for cid in external:
            n = nodes.get(cid)
            if n is None:
                callers.append(CallerRef(cid, cid.split("#", 1)[0], 0, ""))
                continue
            snippet = extract_signature(n.text) or (n.text.splitlines()[0] if n.text else "")
            callers.append(CallerRef(cid, n.path, n.start_line, snippet))
        items.append(ImpactItem(nid, old_sig, new_sig, callers))
    return items


def format_impact(items) -> str:
    """Отчёт о радиусе поражения для MCP-вывода."""
    if not items:
        return "(изменений сигнатур с внешними вызывающими не найдено)"
    blocks = []
    for it in items:
        head = (f"{it.node_id}:\n"
                f"  было:  {it.old_sig}\n"
                f"  стало: {it.new_sig}\n"
                f"  устаревшие вызывающие (вне диффа):")
        rows = "\n".join(f"    - {c.path}:{c.line} | {c.snippet}" for c in it.callers)
        blocks.append(head + "\n" + rows)
    return "\n\n".join(blocks)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/tools/test_impact.py -q`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
git add reviewer/tools/impact.py tests/tools/test_impact.py
git commit -m "feat(tools): compute_impact — движок blast-radius (PRI-126)"
```

---

### Task 3: `ChunkStore.fetch_nodes_at` — выборка текста по конкретному ref

**Files:**
- Modify: `reviewer/index/store.py` (добавить метод после `fetch_nodes`, ~`store.py:354`)
- Test: `tests/index/test_store_hybrid.py` (добавить integration-тест)

**Interfaces:**
- Produces: `ChunkStore.fetch_nodes_at(self, repo, node_ids, ref) -> list[Retrieved]` — чанки строго заданного `ref` (без слияния base/overlay). Используется `compute_impact` для отдельного взятия base- и overlay-версии символа.

- [ ] **Step 1: Write the failing test**

```python
# tests/index/test_store_hybrid.py  (добавить; _row уже определён в файле)
@pytest.mark.integration
def test_fetch_nodes_at_returns_only_given_ref():
    """fetch_nodes_at берёт текст строго указанного ref, не сливая base/overlay."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    d = s.embedding_dim
    vec = [0.0] * d
    store.upsert([
        _row("base:dev", "svc.py", "f", "def f(a, b):\n    ...", vec),
        _row("pr:1", "svc.py", "f", "def f(a, b, c):\n    ...", vec),
    ])
    base = store.fetch_nodes_at("a/x", ["svc.py#f"], "base:dev")
    overlay = store.fetch_nodes_at("a/x", ["svc.py#f"], "pr:1")
    assert [n.text for n in base] == ["def f(a, b):\n    ..."]
    assert [n.text for n in overlay] == ["def f(a, b, c):\n    ..."]
    assert store.fetch_nodes_at("a/x", [], "base:dev") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/index/test_store_hybrid.py::test_fetch_nodes_at_returns_only_given_ref -m integration -q`
Expected: FAIL with `AttributeError: 'ChunkStore' object has no attribute 'fetch_nodes_at'`
(Требует поднятый ParadeDB: `docker compose up -d`.)

- [ ] **Step 3: Write minimal implementation**

```python
# reviewer/index/store.py  (новый метод сразу после fetch_nodes)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/index/test_store_hybrid.py::test_fetch_nodes_at_returns_only_given_ref -m integration -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add reviewer/index/store.py tests/index/test_store_hybrid.py
git commit -m "feat(index): ChunkStore.fetch_nodes_at — выборка по конкретному ref (PRI-126)"
```

---

### Task 4: Тул `get_impact` — 3 слоя

**Files:**
- Modify: `reviewer/tools/code_tools.py:114-132` (добавить замыкание `get_impact`, включить в `raw`)
- Modify: `reviewer/mcp/service.py:295` (метод `get_impact` после `get_changed_file_diff`)
- Modify: `reviewer/entrypoints/mcp_server.py:58-61` (`@mcp.tool() get_impact`)
- Test: `tests/tools/test_impact.py` (регистрация + прогон на фейках)

**Interfaces:**
- Consumes: `compute_impact`, `format_impact` (Task 2); `ToolContext` поля `graph, store, repo, branch, changed_node_ids, changed_paths, overlay_ref`; `MCPReviewService._invoke_tool(repo, pr, name, args)`.
- Produces: tool `get_impact()` (без аргументов) в `make_tools`; `MCPReviewService.get_impact(repo, pr) -> str`; MCP-тул `get_impact(repo, pr) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/tools/test_impact.py  (добавить; _Graph/_Store/_ret определены в Task 2)
def test_get_impact_tool_registered_and_runs():
    from reviewer.tools.code_tools import make_tools, ToolContext
    graph = _Graph({"svc.py#f": ["a.py#g"]})
    store = _Store({
        "pr:1": {"svc.py#f": "def f(a, b, c):\n    ..."},
        "base:dev": {"svc.py#f": "def f(a, b):\n    ...", "a.py#g": "def g():\n    f(1, 2)"},
    })
    ctx = ToolContext(retriever=None, graph=graph, overlay_ref="pr:1",
                      changed_paths=["svc.py"], changed_node_ids=["svc.py#f"],
                      repo="r", branch="dev", store=store)
    tools = {t.name: t for t in make_tools(ctx)}
    assert "get_impact" in tools
    out = tools["get_impact"].invoke({})
    assert "a.py:10" in out and "def f(a, b, c):" in out


def test_get_impact_tool_no_graph():
    from reviewer.tools.code_tools import make_tools, ToolContext
    ctx = ToolContext(retriever=None, graph=None, overlay_ref="pr:1",
                      changed_paths=[], changed_node_ids=[], repo="r", branch="dev")
    tools = {t.name: t for t in make_tools(ctx)}
    assert "недоступн" in tools["get_impact"].invoke({})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/tools/test_impact.py::test_get_impact_tool_registered_and_runs -q`
Expected: FAIL with `KeyError: 'get_impact'`

- [ ] **Step 3a: Add the tool closure in `code_tools.py`**

Вставить замыкание перед строкой `seen: set = set()` (после `get_changed_file_diff`):

```python
    def get_impact() -> str:
        """Радиус поражения PR: символы с ИЗМЕНЁННОЙ сигнатурой → их вызывающие вне диффа.
        Помечает места, которые могут быть не обновлены под новый контракт (кросс-файловый impact).
        Сам не выносит вердикт — подтверждай находки через read_file."""
        from reviewer.tools.impact import compute_impact, format_impact
        if ctx.graph is None or ctx.store is None:
            return "(граф или индекс недоступны)"
        items = compute_impact(
            ctx.graph, ctx.store, repo=ctx.repo, branch=ctx.branch,
            changed_node_ids=ctx.changed_node_ids, changed_paths=ctx.changed_paths,
            overlay_ref=ctx.overlay_ref)
        return format_impact(items)
```

Включить в список `raw`:

```python
    raw = [search_code, get_related_symbols, read_file,
           get_definition, find_callers, get_changed_file_diff, get_impact]
```

- [ ] **Step 3b: Add the service method in `mcp/service.py`** (после `get_changed_file_diff`, ~`service.py:297`)

```python
    def get_impact(self, repo: str, pr: int) -> str:
        """Радиус поражения PR: изменённые сигнатуры → вызывающие вне диффа (impact-анализ)."""
        return self._invoke_tool(repo, pr, "get_impact", {})
```

- [ ] **Step 3c: Register the MCP tool in `entrypoints/mcp_server.py`** (после `get_changed_file_diff`, ~`mcp_server.py:61`)

```python
    @mcp.tool()
    def get_impact(repo: str, pr: int) -> str:
        """Blast-radius: symbols whose signature changed -> their callers outside the PR diff."""
        return service.get_impact(repo, pr)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/tools/test_impact.py -q`
Expected: PASS (14 passed)

- [ ] **Step 5: Lint + commit**

Run: `.venv/bin/ruff check reviewer/tools/ reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py`
Expected: no new errors in touched lines.

```bash
git add reviewer/tools/code_tools.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/tools/test_impact.py
git commit -m "feat(tools): MCP-тул get_impact — 3 слоя (PRI-126)"
```

---

### Task 5: Измерение blast-radius в плагине

**Files:**
- Create: `plugin/skills/review-pr/references/blast-radius-prompt.md`
- Modify: `plugin/skills/review-pr/SKILL.md` (шаг 4 «Dimensions»; шаг 5 «Verify» — список тулов)

**Interfaces:**
- Consumes: MCP-тул `get_impact(repo, pr)` (Task 4); JSON-схема находок из `references/analyze-prompt.md` (category `correctness`).
- Produces: новый whole-diff субагент, выдающий находки `category=correctness`, якорённые на строку изменённой сигнатуры.

- [ ] **Step 1: Create the reference prompt**

```markdown
<!-- plugin/skills/review-pr/references/blast-radius-prompt.md -->
You are a senior reviewer measuring the BLAST RADIUS of a pull request: cross-file
contract breaks that per-file review misses. A changed function signature can break
its callers in OTHER files that the diff never touched.

Method:
- Call `get_impact(repo, pr)` ONCE. It returns, for each symbol whose signature
  actually changed (gated base-vs-head), the old/new signature and the callers that
  live OUTSIDE the diff (`path:line` of the calling symbol + its header).
- `get_impact` does NOT decide breakage — it gives facts. For each reported caller,
  decide whether the new signature actually breaks it:
  use `read_file(path, start, end)` to inspect the call site and
  `get_changed_file_diff(path)` to confirm the caller was NOT updated in this PR.
- A new REQUIRED parameter (no default), a removed/renamed parameter, or a changed
  parameter order breaks positional/keyword callers → report. A new parameter WITH a
  default, or a purely internal body change, usually does NOT → skip.
- Recall depends on graph completeness (tree-sitter in live review may miss dynamic
  or aliased calls). Frame findings as concrete but verify each via `read_file`.
  If `get_impact` returns "(… не найдено)", there is nothing to report — return an
  empty findings list.

Anchoring (important): the stale callers live OUTSIDE the diff, where GitHub forbids
inline comments. So anchor each finding on the CHANGED SIGNATURE line:
- `file` = the changed file, `side: RIGHT`;
- `code_quote` = the new `def`/`async def` header line, copied verbatim from the new file;
- `line` = a number from `commentable_right` on that header;
- `message` = describe the contract change and ENUMERATE the stale callers
  (`path:line`) that need updating;
- one finding per changed signature (do not split per caller).

Return ONLY a JSON object in the schema of `analyze-prompt.md`, with
`category: "correctness"`. Write `message`/`suggestion` in the orchestrator's output
language. An empty findings list is a valid result.
```

- [ ] **Step 2: Add the dimension to SKILL.md step 4**

В `plugin/skills/review-pr/SKILL.md`, в шаге 4, после буллета `requirements (...)` (перед строкой `Give the performance/maintainability subagents:`) вставить:

```markdown
   - blast-radius: dispatch one subagent with `references/blast-radius-prompt.md`, the diffs of
     all units (path + patch), the PR `title`/`body`, the repo/pr identifiers (so it can call the
     reviewer MCP tools, including `get_impact`), and the target output language. It returns the
     same findings JSON schema with category `correctness`.
```

- [ ] **Step 3: Allow `get_impact` in the verify step**

В `SKILL.md`, шаг 5 «Verify», в перечислении тулов заменить строку:

```markdown
   (`read_file`, `search_code`, `find_callers`, `get_definition`). It returns
```

на:

```markdown
   (`read_file`, `search_code`, `find_callers`, `get_definition`, `get_impact`). It returns
```

- [ ] **Step 4: Manual verification**

Run: `grep -n "blast-radius\|get_impact" plugin/skills/review-pr/SKILL.md plugin/skills/review-pr/references/blast-radius-prompt.md`
Expected: dimension bullet present in SKILL.md (step 4), `get_impact` in step 5 tool list, prompt file exists with anchoring instructions.

- [ ] **Step 5: Commit**

```bash
git add plugin/skills/review-pr/SKILL.md plugin/skills/review-pr/references/blast-radius-prompt.md
git commit -m "feat(review-pr): измерение blast-radius (PRI-126)"
```

---

### Task 6: Полный прогон и проверка регрессий

**Files:** — (без изменений кода; финальная верификация)

- [ ] **Step 1: Run the full unit suite**

Run: `.venv/bin/pytest -q`
Expected: PASS, включая новые `tests/tools/test_impact.py` (14) и без регрессий в `tests/tools/test_code_tools.py`.

- [ ] **Step 2: Run integration (требует `docker compose up -d`)**

Run: `.venv/bin/pytest -m integration tests/index/test_store_hybrid.py -q`
Expected: PASS, включая `test_fetch_nodes_at_returns_only_given_ref`.

- [ ] **Step 3: Lint**

Run: `.venv/bin/ruff check reviewer/tools/impact.py reviewer/tools/code_tools.py reviewer/index/store.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py`
Expected: clean for touched files.

- [ ] **Step 4: Финализация ветки**

Готово к PR `feat/pri-126-blast-radius` → `dev`. Хэндофф в `superpowers:finishing-a-development-branch`.

---

## Self-Review (выполнено при написании плана)

**Spec coverage:**
- `get_impact` тул (3 слоя) → Task 4. ✔
- Движок `impact.py` (extract_signature, compute_impact) → Tasks 1–2. ✔
- Сигнатурный гейт base-vs-overlay → Task 2 (`compute_impact`, тест `gate_skips_body_only_change`). ✔
- `fetch_nodes_at` для раздельного base/overlay → Task 3. ✔
- Измерение blast-radius (шаг 4) + reference-промпт → Task 5. ✔
- Якорение на строку сигнатуры (обход inline-ограничения GitHub) → Task 5 (промпт). ✔
- Критерий приёмки (сигнатура с N вызывающими → находка) → Task 2 (`flags_external_callers_on_signature_change`). ✔
- Известное ограничение (tree-sitter recall, «потенциально устаревшие» + verify через read_file) → Task 5 (промпт). ✔

**Type consistency:** `compute_impact`/`format_impact`/`ImpactItem`/`CallerRef`/`extract_signature`/`fetch_nodes_at` — имена и сигнатуры совпадают между задачами и тестами. `base_ref(branch)` → `"base:<branch>"` (используется и в фейк-сторе тестов как ключ `"base:dev"`).

**Placeholders:** нет — весь код и команды конкретны.
