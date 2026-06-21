# Компактный вывод графовых тулов (find_callers / get_related_symbols) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `find_callers` и `get_related_symbols` (оба контура: сессионный PR-ревью и session-less) выдают на каждый элемент `node_id` + `(file:line)` + однострочный сниппет + тип ребра (CALLS/IMPLEMENTS/TESTED_BY), вместо сырых `node_id`.

**Architecture:** Подход A — аддитивные detailed-методы граф-стора (`callers_detailed`/`expand_detailed`) отдают связи с типом ребра и дистанцией; общий форматтер `reviewer/tools/graph_format.py` джойнит их с Postgres (`store.fetch_nodes`) и рендерит компактные строки. Старые `expand`/`callers` (`set[str]`) не трогаем — их держат ретрив (`retriever.py:92,128`) и impact (`impact.py:84`).

**Tech Stack:** Python 3.11, Neo4j (Cypher), pytest (маркер `integration` для тестов с Neo4j), langchain StructuredTool, FastMCP.

## Global Constraints

- Язык проекта — русский: докстринги, сообщения, комментарии.
- Линт: `ruff check .`, line-length 100, target py311.
- Коммиты: Conventional Commits на русском, **без** self-attribution (никаких Co-Authored-By/Claude).
- Юнит-тесты не дёргают внешние сервисы (фейки/моки); тесты с Neo4j помечаются `@pytest.mark.integration` и запускаются `pytest -m integration`.
- `pytest` по умолчанию исключает integration (`addopts = -m 'not integration'`).
- Инвариант `node_id = "path#fqn"` — ключ джойна графа (Neo4j) и чанков (Postgres).
- Сигнатуры `GraphStore.expand` / `GraphStore.callers` (возвращают `set[str]`) НЕ менять — переиспользуются `retriever.py` и `impact.py`.

---

### Task 1: detailed-методы граф-стора (`callers_detailed`, `expand_detailed`)

**Files:**
- Modify: `reviewer/graph/store.py` (добавить после `callers`, ~`store.py:94`)
- Test: `tests/graph/test_store.py` (integration, Neo4j)

**Interfaces:**
- Produces:
  - `GraphStore.callers_detailed(repo: str, node_ids: list[str], *, branch: str = "") -> list[dict]` — элементы `{"id": str, "rel": "CALLS"}`, упорядочены по `id`.
  - `GraphStore.expand_detailed(repo: str, node_ids: list[str], hops: int = 2, *, branch: str = "") -> list[dict]` — элементы `{"id": str, "rels": list[str], "dist": int}`, упорядочены по `(dist, id)`; сам seed исключён (`n.id <> sid`).

- [ ] **Step 1: Написать падающие integration-тесты**

В `tests/graph/test_store.py` добавить в конец файла:

```python
@pytest.mark.integration
def test_callers_detailed_returns_rel_and_id(graph_store):
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes("test/repo", ["a.py#f", "a.py#g", "a.py#h"])
    graph_store.upsert_edges("test/repo", [
        ("a.py#g", "CALLS", "a.py#f"),
        ("a.py#h", "CALLS", "a.py#f"),
    ])
    out = graph_store.callers_detailed("test/repo", ["a.py#f"])
    assert out == [
        {"id": "a.py#g", "rel": "CALLS"},
        {"id": "a.py#h", "rel": "CALLS"},
    ]
    assert graph_store.callers_detailed("test/repo", ["a.py#g"]) == []


@pytest.mark.integration
def test_expand_detailed_rels_distance_and_self_excluded(graph_store):
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes(
        "test/repo", ["base.py#A", "impl.py#B", "call.py#C", "deep.py#D"])
    graph_store.upsert_edges("test/repo", [
        ("impl.py#B", "IMPLEMENTS", "base.py#A"),   # B -[IMPLEMENTS]-> A  (d1)
        ("call.py#C", "CALLS", "base.py#A"),        # C -[CALLS]-> A       (d1)
        ("deep.py#D", "CALLS", "call.py#C"),        # D -[CALLS]-> C       (A..D = d2)
    ])
    out = graph_store.expand_detailed("test/repo", ["base.py#A"], hops=2)
    by_id = {r["id"]: r for r in out}
    assert "base.py#A" not in by_id                  # self исключён
    assert by_id["impl.py#B"]["dist"] == 1 and by_id["impl.py#B"]["rels"] == ["IMPLEMENTS"]
    assert by_id["call.py#C"]["dist"] == 1 and by_id["call.py#C"]["rels"] == ["CALLS"]
    assert by_id["deep.py#D"]["dist"] == 2
    # порядок — по (dist, id): d1 раньше d2
    assert [r["id"] for r in out][-1] == "deep.py#D"
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest -m integration tests/graph/test_store.py::test_callers_detailed_returns_rel_and_id tests/graph/test_store.py::test_expand_detailed_rels_distance_and_self_excluded -v`
Expected: FAIL — `AttributeError: 'GraphStore' object has no attribute 'callers_detailed'`.

(Требует поднятого Neo4j: `docker compose up -d`.)

- [ ] **Step 3: Реализовать методы**

В `reviewer/graph/store.py` сразу после метода `callers` (после строки ~94) добавить:

```python
    def callers_detailed(self, repo: str, node_ids: list[str], *,
                         branch: str = "") -> list[dict]:
        """Вызывающие символы с типом ребра — входящие CALLS.
        Элементы: {"id": <node_id>, "rel": "CALLS"}, упорядочены по id."""
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid "
            "MATCH (c:Symbol {repo: $repo, branch: $branch})-[:CALLS]->"
            "(s:Symbol {repo: $repo, branch: $branch, id: sid}) "
            "RETURN DISTINCT c.id AS id ORDER BY id",
            ids=list(node_ids), repo=repo, branch=branch)
        return [{"id": r["id"], "rel": "CALLS"} for r in records]

    def expand_detailed(self, repo: str, node_ids: list[str], hops: int = 2, *,
                        branch: str = "") -> list[dict]:
        """Соседи символа с типами рёбер кратчайшего пути и дистанцией.
        Элементы: {"id", "rels": [тип,...], "dist": int}; seed исключён;
        упорядочены по (dist, id)."""
        records, _, _ = self._driver.execute_query(
            f"UNWIND $ids AS sid "
            f"MATCH (s:Symbol {{repo: $repo, branch: $branch, id: sid}}) "
            f"MATCH p=(s)-[:CALLS|IMPLEMENTS|TESTED_BY*1..{hops}]-"
            f"(n:Symbol {{repo: $repo, branch: $branch}}) "
            f"WHERE n.id <> sid "
            f"WITH n.id AS id, [r IN relationships(p) | type(r)] AS rels, length(p) AS dist "
            f"ORDER BY dist "
            f"WITH id, collect({{rels: rels, dist: dist}})[0] AS best "
            f"RETURN id, best.rels AS rels, best.dist AS dist "
            f"ORDER BY best.dist, id",
            ids=list(node_ids), repo=repo, branch=branch)
        return [{"id": r["id"], "rels": list(r["rels"]), "dist": r["dist"]} for r in records]
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest -m integration tests/graph/test_store.py -v`
Expected: PASS (все, включая старые `test_upsert_and_expand`, `test_callers_directed`).

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/graph/store.py tests/graph/test_store.py
git add reviewer/graph/store.py tests/graph/test_store.py
git commit -m "feat(graph): detailed-методы callers_detailed/expand_detailed (PRI-148)"
```

---

### Task 2: общий форматтер `format_neighbors`

**Files:**
- Create: `reviewer/tools/graph_format.py`
- Test: `tests/tools/test_graph_format.py` (unit, фейк-стор)

**Interfaces:**
- Consumes: элементы из Task 1 (`{"id","rel"}` от `callers_detailed`; `{"id","rels","dist"}` от `expand_detailed`); `store.fetch_nodes(repo, ids, overlay_ref, changed_paths, *, base_ref) -> list[Retrieved]` (поля `node_id`, `path`, `start_line`, `text`); `reviewer.index.refs.base_ref`.
- Produces: `format_neighbors(neighbors: list[dict], *, store, repo: str, branch: str, overlay_ref, changed_paths: list[str], empty_msg: str) -> str`.

- [ ] **Step 1: Написать падающие unit-тесты**

Создать `tests/tools/test_graph_format.py`:

```python
from reviewer.tools.graph_format import format_neighbors
from reviewer.index.store import Retrieved


class EchoStore:
    """fetch_nodes возвращает Retrieved для каждого запрошенного id (path:10)."""
    def __init__(self, known=None):
        self.known = known  # None => все известны; set => только эти
        self.last = None

    def fetch_nodes(self, repo, ids, overlay_ref, changed_paths, *, base_ref="base"):
        self.last = dict(repo=repo, ids=list(ids), overlay_ref=overlay_ref,
                         changed_paths=list(changed_paths), base_ref=base_ref)
        out = []
        for i in ids:
            if self.known is not None and i not in self.known:
                continue
            name = i.split("#", 1)[1]
            out.append(Retrieved(i, i.split("#", 1)[0], name, "function",
                                 10, 12, f"def {name}():\n    return 1", 0.0))
        return out


def test_empty_returns_empty_msg():
    out = format_neighbors([], store=EchoStore(), repo="a/b", branch="main",
                           overlay_ref=None, changed_paths=[], empty_msg="(нет связей)")
    assert out == "(нет связей)"


def test_callers_format_has_fileline_snippet_and_rel():
    out = format_neighbors(
        [{"id": "x.py#caller", "rel": "CALLS"}],
        store=EchoStore(), repo="a/b", branch="main",
        overlay_ref=None, changed_paths=[], empty_msg="(вызовов не найдено)")
    assert "// x.py#caller (x.py:10) [CALLS]" in out
    assert "def caller():" in out


def test_related_format_has_rels_and_distance():
    out = format_neighbors(
        [{"id": "i.py#B", "rels": ["IMPLEMENTS"], "dist": 1},
         {"id": "d.py#D", "rels": ["CALLS", "CALLS"], "dist": 2}],
        store=EchoStore(), repo="a/b", branch="main",
        overlay_ref=None, changed_paths=[], empty_msg="(нет связей)")
    assert "// i.py#B (i.py:10) [IMPLEMENTS, d1]" in out
    assert "// d.py#D (d.py:10) [CALLS, d2]" in out   # повтор типа схлопнут


def test_missing_in_index_keeps_id_with_note():
    out = format_neighbors(
        [{"id": "gone.py#z", "rel": "CALLS"}],
        store=EchoStore(known=set()), repo="a/b", branch="main",
        overlay_ref=None, changed_paths=[], empty_msg="(вызовов не найдено)")
    assert "// gone.py#z [CALLS] (вне индекса)" in out


def test_store_none_degrades_to_id_and_rel():
    out = format_neighbors(
        [{"id": "a.py#f", "rel": "CALLS"}],
        store=None, repo="a/b", branch="main",
        overlay_ref=None, changed_paths=[], empty_msg="(вызовов не найдено)")
    assert out == "// a.py#f [CALLS]"


def test_cap_truncates_with_note():
    neighbors = [{"id": f"m.py#f{i}", "rel": "CALLS"} for i in range(30)]
    out = format_neighbors(neighbors, store=EchoStore(), repo="a/b", branch="main",
                           overlay_ref=None, changed_paths=[], empty_msg="x")
    assert "m.py#f24" in out and "m.py#f25" not in out
    assert "(…ещё 5, усечено)" in out


def test_fetch_nodes_called_with_base_ref_for_branch():
    store = EchoStore()
    format_neighbors([{"id": "a.py#f", "rel": "CALLS"}],
                     store=store, repo="a/b", branch="dev",
                     overlay_ref="pr:9", changed_paths=["a.py"], empty_msg="x")
    assert store.last["base_ref"] == "base:dev"
    assert store.last["overlay_ref"] == "pr:9"
    assert store.last["changed_paths"] == ["a.py"]
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tools/test_graph_format.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.tools.graph_format'`.

- [ ] **Step 3: Реализовать форматтер**

Создать `reviewer/tools/graph_format.py`:

```python
from __future__ import annotations

from reviewer.index.refs import base_ref

_CAP = 25


def _rel_label(nb: dict) -> str:
    """Метка связи: callers → 'CALLS'; related → 'CALLS→IMPLEMENTS, d2'."""
    if "rels" in nb:
        seen: list[str] = []
        for r in nb.get("rels") or []:
            if r not in seen:
                seen.append(r)
        types = "→".join(seen) if seen else "?"
        return f"{types}, d{nb.get('dist', '?')}"
    return nb.get("rel", "?")


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line
    return ""


def format_neighbors(neighbors: list[dict], *, store, repo: str, branch: str,
                     overlay_ref, changed_paths: list[str], empty_msg: str) -> str:
    """Рендер соседей графа: '// id (path:line) [REL]\\n<сниппет>'.

    Сниппет — строка определения символа из Postgres (store.fetch_nodes).
    store=None → деградация к 'id [REL]'; промах индекса → '… (вне индекса)';
    кап _CAP элементов, хвост '(…ещё N, усечено)'. Порядок сохраняется как есть.
    """
    if not neighbors:
        return empty_msg
    total = len(neighbors)
    items = neighbors[:_CAP]
    nodes: dict = {}
    if store is not None:
        try:
            fetched = store.fetch_nodes(repo, [n["id"] for n in items],
                                        overlay_ref, changed_paths,
                                        base_ref=base_ref(branch))
            nodes = {n.node_id: n for n in fetched}
        except Exception:
            nodes = {}
    lines: list[str] = []
    for nb in items:
        rel = _rel_label(nb)
        meta = nodes.get(nb["id"])
        if meta is not None:
            lines.append(
                f"// {nb['id']} ({meta.path}:{meta.start_line}) [{rel}]\n"
                f"{_first_line(meta.text)}")
        elif store is None:
            lines.append(f"// {nb['id']} [{rel}]")
        else:
            lines.append(f"// {nb['id']} [{rel}] (вне индекса)")
    if total > _CAP:
        lines.append(f"(…ещё {total - _CAP}, усечено)")
    return "\n".join(lines)
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tools/test_graph_format.py -v`
Expected: PASS (7 тестов).

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/tools/graph_format.py tests/tools/test_graph_format.py
git add reviewer/tools/graph_format.py tests/tools/test_graph_format.py
git commit -m "feat(tools): общий форматтер graph_format.format_neighbors (PRI-148)"
```

---

### Task 3: развод сессионных тулов (`code_tools.py`)

**Files:**
- Modify: `reviewer/tools/code_tools.py:67-70` (`get_related_symbols`), `:114-119` (`find_callers`)
- Test: `tests/tools/test_code_tools.py`

**Interfaces:**
- Consumes: `format_neighbors` (Task 2); `ctx.graph.expand_detailed` / `ctx.graph.callers_detailed` (Task 1); `ctx.store`, `ctx.overlay_ref`, `ctx.changed_paths`, `ctx.branch`, `ctx.repo` (уже в `ToolContext`).
- Produces: тулы `get_related_symbols` / `find_callers` с компактной выдачей.

- [ ] **Step 1: Обновить фейки и тесты под новый формат**

В `tests/tools/test_code_tools.py`:

(a) Заменить `class FakeGraph` (строки 8-9) на:

```python
class FakeGraph:
    def expand(self, repo, ids, hops=2, *, branch=""): return {"b.py#g"}
    def expand_detailed(self, repo, ids, hops=2, *, branch=""):
        return [{"id": "b.py#g", "rels": ["CALLS"], "dist": 1}]
    def callers_detailed(self, repo, ids, *, branch=""):
        return [{"id": "x.py#caller", "rel": "CALLS"}]
```

(b) Заменить `class FakeGraphRich` (строки 28-31) на:

```python
class FakeGraphRich:
    def expand(self, repo, ids, hops=2, *, branch=""): return {"b.py#g"}
    def callers(self, repo, ids, *, branch=""): return {"x.py#caller"}
    def find_symbol(self, repo, name, *, branch=""): return ["a.py#f"]
    def expand_detailed(self, repo, ids, hops=2, *, branch=""):
        return [{"id": "b.py#g", "rels": ["CALLS"], "dist": 1}]
    def callers_detailed(self, repo, ids, *, branch=""):
        return [{"id": "x.py#caller", "rel": "CALLS"}]
```

(c) Заменить `class FakeStore` (строки 33-36) на эхо-стор (рендерит сниппет на любой id):

```python
class FakeStore:
    def fetch_nodes(self, repo, ids, overlay_ref, changed_paths, *, base_ref="base"):
        from reviewer.index.store import Retrieved
        return [Retrieved(i, i.split("#", 1)[0], i.split("#", 1)[1], "function",
                          10, 12, f"def {i.split('#', 1)[1]}():\n    return 1", 0.0)
                for i in ids]
```

(d) Заменить тело теста `test_find_callers_directed` (строки 67-70) на проверку нового формата:

```python
def test_find_callers_directed():
    tools = {t.name: t for t in make_tools(_rich_ctx())}
    out = tools["find_callers"].invoke({"node_id": "a.py#f"})
    assert "// x.py#caller (x.py:10) [CALLS]" in out
    assert "def caller():" in out
```

(e) В тесте `test_tools_thread_repo_to_graph_and_retriever` заменить класс `G` (строки 196-205) — тулы теперь зовут `*_detailed`:

```python
    class G:
        def expand_detailed(self, repo, ids, hops=2, *, branch=""):
            calls["expand_repo"] = repo
            return []
        def callers_detailed(self, repo, ids, *, branch=""):
            calls["callers_repo"] = repo
            return []
        def find_symbol(self, repo, name, *, branch=""):
            calls["find_repo"] = repo
            return []
```

(`test_get_callers_tool_uses_graph` и `test_get_definition_uses_graph_and_store` менять не нужно: первый проверяет `"b.py#g" in out` — id остаётся в выдаче; второй опирается на `get_definition`, который не меняется, а эхо-стор для id `a.py#f` даёт `def f` в тексте.)

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tools/test_code_tools.py -v`
Expected: FAIL — `AttributeError` на `expand_detailed`/`callers_detailed` ещё не вызываются в тулах (старый код зовёт `expand`/`callers`), `test_find_callers_directed` падает на новом формате.

- [ ] **Step 3: Переписать тулы**

В `reviewer/tools/code_tools.py` добавить импорт (после строки 10):

```python
from reviewer.tools.graph_format import format_neighbors
```

Заменить `get_related_symbols` (строки 67-70) на:

```python
    def get_related_symbols(node_id: str) -> str:
        """Связанные символы (вызовы/реализации/тесты) для node_id 'path#fqn'.
        На элемент: file:line + строка определения + тип ребра (CALLS/IMPLEMENTS/TESTED_BY) и дистанция."""
        if ctx.graph is None or not hasattr(ctx.graph, "expand_detailed"):
            return "(граф недоступен)"
        neighbors = ctx.graph.expand_detailed(ctx.repo, [node_id], hops=2, branch=ctx.branch)
        return format_neighbors(
            neighbors, store=ctx.store, repo=ctx.repo, branch=ctx.branch,
            overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths,
            empty_msg="(нет связей)")
```

Заменить `find_callers` (строки 114-119) на:

```python
    def find_callers(node_id: str) -> str:
        """Кто вызывает символ node_id ('path#fqn') — входящие CALLS (impact-анализ).
        На элемент: file:line + строка определения вызывающего + [CALLS]."""
        if ctx.graph is None or not hasattr(ctx.graph, "callers_detailed"):
            return "(граф недоступен)"
        found = ctx.graph.callers_detailed(ctx.repo, [node_id], branch=ctx.branch)
        return format_neighbors(
            found, store=ctx.store, repo=ctx.repo, branch=ctx.branch,
            overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths,
            empty_msg="(вызовов не найдено)")
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tools/test_code_tools.py -v`
Expected: PASS (все тесты файла).

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/tools/code_tools.py tests/tools/test_code_tools.py
git add reviewer/tools/code_tools.py tests/tools/test_code_tools.py
git commit -m "feat(tools): компактная выдача get_related_symbols/find_callers (PRI-148)"
```

---

### Task 4: session-less контур (`service.py`) + докстринги тулов (`mcp_server.py`)

**Files:**
- Modify: `reviewer/mcp/service.py:411-426` (`related_symbols`), `:428-443` (`callers`)
- Modify: `reviewer/entrypoints/mcp_server.py` (докстринги `get_related_symbols`, `find_callers`, `related_symbols`, `callers`)
- Test: `tests/mcp/test_service.py`

**Interfaces:**
- Consumes: `format_neighbors` (Task 2); `self.components.graph.expand_detailed` / `callers_detailed` (Task 1); `self.components.store`.
- Produces: session-less `related_symbols` / `callers` с компактной выдачей.

- [ ] **Step 1: Обновить моки и тесты session-less методов**

В `tests/mcp/test_service.py`:

(a) В `_components()` (после строки 54 `c.graph.find_symbol.return_value = []`) добавить:

```python
    c.graph.expand_detailed.return_value = []
    c.graph.callers_detailed.return_value = []
```

(b) Заменить `test_related_symbols_delegates_to_graph` (строки 550-557) на:

```python
def test_related_symbols_delegates_to_graph() -> None:
    """related_symbols зовёт graph.expand_detailed и форматирует компактно."""
    svc = _make_mcp_service()
    svc.components.graph.expand_detailed.return_value = [
        {"id": "a.py#bar", "rels": ["CALLS"], "dist": 1},
        {"id": "a.py#baz", "rels": ["IMPLEMENTS"], "dist": 1},
    ]
    svc.components.store.fetch_nodes.return_value = [
        SimpleNamespace(node_id="a.py#bar", path="a.py", start_line=5,
                        end_line=6, text="def bar(): ..."),
        SimpleNamespace(node_id="a.py#baz", path="a.py", start_line=9,
                        end_line=10, text="def baz(): ..."),
    ]
    out = svc.related_symbols("a/b", "a.py#foo")
    assert "// a.py#bar (a.py:5) [CALLS, d1]" in out
    assert "// a.py#baz (a.py:9) [IMPLEMENTS, d1]" in out
    svc.components.graph.expand_detailed.assert_called_once_with(
        "a/b", ["a.py#foo"], hops=2, branch=svc.settings.primary_branch())
```

(c) Заменить `test_related_symbols_empty_and_failsoft` (строки 560-566) на:

```python
def test_related_symbols_empty_and_failsoft() -> None:
    """Пусто → '(нет связей)'; исключение графа → тоже заглушка, не падаем."""
    svc = _make_mcp_service()
    svc.components.graph.expand_detailed.return_value = []
    assert svc.related_symbols("a/b", "a.py#foo") == "(нет связей)"
    svc.components.graph.expand_detailed.side_effect = RuntimeError("neo4j down")
    assert svc.related_symbols("a/b", "a.py#foo") == "(нет связей)"
```

(d) Заменить `test_callers_delegates_to_graph` (строки 583-592) на:

```python
def test_callers_delegates_to_graph() -> None:
    """callers зовёт graph.callers_detailed и форматирует компактно."""
    svc = _make_mcp_service()
    svc.components.graph.callers_detailed.return_value = [
        {"id": "a.py#caller", "rel": "CALLS"}]
    svc.components.store.fetch_nodes.return_value = [
        SimpleNamespace(node_id="a.py#caller", path="a.py", start_line=3,
                        end_line=4, text="def caller(): ...")]
    out = svc.callers("a/b", "a.py#foo")
    assert "// a.py#caller (a.py:3) [CALLS]" in out
    svc.components.graph.callers_detailed.assert_called_once_with(
        "a/b", ["a.py#foo"], branch=svc.settings.primary_branch())
    svc.components.graph.callers_detailed.return_value = []
    assert svc.callers("a/b", "a.py#foo") == "(вызовов не найдено)"
```

(`test_related_symbols_graph_none` и `test_related_symbols_invalid_branch` менять не нужно — поведение гардов сохраняется.)

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_service.py -k "related_symbols or callers" -v`
Expected: FAIL — `related_symbols`/`callers` ещё зовут `graph.expand`/`graph.callers`, формат старый.

- [ ] **Step 3: Переписать session-less методы**

В `reviewer/mcp/service.py` добавить импорт рядом с импортом code_tools (после строки 31 `from reviewer.tools.code_tools import ToolContext, make_tools`):

```python
from reviewer.tools.graph_format import format_neighbors
```

Заменить `related_symbols` (строки 411-426) на:

```python
    def related_symbols(self, repo: str, node_id: str,
                        branch: str | None = None) -> str:
        """Соседи символа по графу (calls/implements/tests) без PR-сессии.
        На элемент: file:line + строка определения + тип ребра и дистанция."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        if self.components.graph is None:
            return "(граф недоступен)"
        try:
            neighbors = self.components.graph.expand_detailed(
                repo, [node_id], hops=2, branch=resolved)
        except Exception:
            log.warning("related_symbols: сбой графа", exc_info=True)
            return "(нет связей)"
        return format_neighbors(
            neighbors, store=self.components.store, repo=repo, branch=resolved,
            overlay_ref=None, changed_paths=[], empty_msg="(нет связей)")
```

Заменить `callers` (строки 428-443) на:

```python
    def callers(self, repo: str, node_id: str,
                branch: str | None = None) -> str:
        """Кто вызывает символ node_id ('path#fqn') — входящие CALLS, без PR-сессии.
        На элемент: file:line + строка определения вызывающего + [CALLS]."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        if self.components.graph is None:
            return "(граф недоступен)"
        try:
            found = self.components.graph.callers_detailed(
                repo, [node_id], branch=resolved)
        except Exception:
            log.warning("callers: сбой графа", exc_info=True)
            return "(вызовов не найдено)"
        return format_neighbors(
            found, store=self.components.store, repo=repo, branch=resolved,
            overlay_ref=None, changed_paths=[], empty_msg="(вызовов не найдено)")
```

- [ ] **Step 4: Обновить докстринги тулов в `mcp_server.py`**

В `reviewer/entrypoints/mcp_server.py`:

`get_related_symbols` (строка ~39):
```python
        """Code-graph neighbors of a symbol node_id 'path#fqn'.
        Each item: node_id + (file:line) + one-line definition snippet + edge type
        (CALLS/IMPLEMENTS/TESTED_BY) and hop distance."""
```

`find_callers` (строка ~55):
```python
        """Direct callers of a symbol node_id.
        Each item: node_id + (file:line) + one-line snippet of the caller's
        definition + [CALLS]."""
```

`related_symbols` (session-less, строка ~149):
```python
        """Code-graph neighbors (calls/implementations/tests) of a symbol node_id
        'path#fqn' over the base index (no PR session). Each item: node_id +
        (file:line) + one-line definition snippet + edge type and distance.
        branch defaults to the primary tracked branch."""
```

`callers` (session-less, строка ~155):
```python
        """Direct callers of a symbol node_id 'path#fqn' over the base index
        (no PR session). Each item: node_id + (file:line) + one-line snippet of
        the caller's definition + [CALLS]. branch defaults to the primary tracked branch."""
```

- [ ] **Step 5: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/mcp/test_service.py tests/mcp/test_server_tools.py -v`
Expected: PASS. (Форвардинг-тесты в `test_server_tools.py` мокают сервисный слой и не затрагиваются — проверяем, что они по-прежнему зелёные.)

- [ ] **Step 6: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_service.py
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_service.py
git commit -m "feat(mcp): компактная выдача session-less related_symbols/callers + докстринги (PRI-148)"
```

---

### Task 5: финальная проверка всего набора

**Files:** —

- [ ] **Step 1: Полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены по умолчанию). Особо проверить, что `tests/tools/test_impact.py` зелёный (использует `callers()` → `set[str]`, не менялся).

- [ ] **Step 2: Integration-прогон граф-стора (нужен Neo4j)**

Run: `docker compose up -d && .venv/bin/pytest -m integration tests/graph/test_store.py -v`
Expected: PASS.

- [ ] **Step 3: Линт всего пакета**

Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: без новых ошибок в затронутых файлах.

- [ ] **Step 4: Финальный коммит (если остались правки)**

```bash
git add -A && git commit -m "test(graph-tools): финальная проверка компактной выдачи (PRI-148)"
```

(Если правок нет — пропустить.)

---

## Self-Review

**1. Spec coverage:**
- detailed-методы графа (тип ребра + дистанция) → Task 1. ✓
- общий форматтер file:line + сниппет + тип ребра, кап, дегрейд → Task 2. ✓
- сессионный контур (`code_tools.py`) → Task 3. ✓
- session-less контур (`service.py`) + докстринги (`mcp_server.py`) → Task 4. ✓
- `expand`/`callers` (`set[str]`) не тронуты, `impact.py`/`retriever.py` целы → Constraint + Task 5 Step 1. ✓
- тесты: unit форматтера, integration detailed-методов, обновление существующих → Tasks 1,2,3,4. ✓
- Вне объёма (точная строка call-site) — не планируется. ✓

**2. Placeholder scan:** код приведён полностью в каждом шаге; плейсхолдеров нет.

**3. Type consistency:** `callers_detailed → {"id","rel"}`, `expand_detailed → {"id","rels","dist"}` — `format_neighbors._rel_label` разбирает оба; `fetch_nodes(..., base_ref=base_ref(branch))` — сигнатура совпадает с `reviewer/index/store.py:335`; `Retrieved` поля (`node_id/path/start_line/text`) совпадают с используемыми. Имена методов едины во всех тасках.
