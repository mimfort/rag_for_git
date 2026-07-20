# PRI-179 — directed `implementations` graph tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить directed session-less MCP-тул `implementations(node_id)` — входящие рёбра `IMPLEMENTS` (наследники/реализации символа) поверх base-графа, для graph-deepening в solve-task на OO/registry-задачах.

**Architecture:** Клонируем существующую directed-цепочку `callers` (store → service → mcp_server), меняя ребро `CALLS` на `IMPLEMENTS`. Рёбра `IMPLEMENTS` уже строит SCIP-бэкенд на полном `reviewer index` (подтверждено спайком Phase 0). Построение/синхронизацию графа (`builder.py`, `graph_sync.py`) НЕ трогаем — Вариант A.

**Tech Stack:** Python 3.11+, Neo4j (Cypher, драйвер neo4j), FastMCP, pytest (unit + `-m integration`), Voyage (не затрагивается).

## Global Constraints

- Русские докстринги/комментарии/сообщения об ошибках в новом коде.
- Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- Ruff line-length 100, target py311 (`.venv/bin/ruff check .`).
- Направление тула — только **входящие** `IMPLEMENTS` (наследники/реализации X). Суперклассы X не покрываем.
- Результат-контракт `implementations_detailed` идентичен `callers_detailed`: `list[{"id": str, "rel": "IMPLEMENTS"}]`, `ORDER BY id` — чтобы переиспользовать `format_neighbors` без правок.
- `graph_sync.py` и `builder.py` не изменять. PR-session вариант тула (`reviewer/tools/`) не добавлять.
- Fail-soft сообщения тула: `(граф недоступен)` при `graph is None`; `(implementations не найдены)` при пустоте/исключении.

---

### Task 1: `implementations_detailed` в GraphStore

**Files:**
- Modify: `reviewer/graph/store.py` (добавить метод после `callers_detailed`, ~строка 106)
- Test: `tests/graph/test_store.py` (добавить 2 integration-теста после `test_callers_detailed_returns_rel_and_id`, ~строка 113)

**Interfaces:**
- Consumes: `GraphStore.upsert_nodes(repo, node_ids, *, branch="")`, `GraphStore.upsert_edges(repo, edges, *, branch="")`, `GraphStore.init_schema()`, `GraphStore.clear()` (существуют).
- Produces: `GraphStore.implementations_detailed(repo: str, node_ids: list[str], *, branch: str = "") -> list[dict]` — входящие `IMPLEMENTS`, элементы `{"id": <node_id>, "rel": "IMPLEMENTS"}`, `ORDER BY id`. Пусто → `[]`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/graph/test_store.py`, сразу после `test_callers_detailed_returns_rel_and_id` (после строки 113):

```python
@pytest.mark.integration
def test_implementations_detailed_returns_rel_and_id(graph_store):
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes(
        "test/repo", ["base.py#Base", "impl.py#Sub", "impl.py#Other"])
    graph_store.upsert_edges("test/repo", [
        ("impl.py#Sub", "IMPLEMENTS", "base.py#Base"),
        ("impl.py#Other", "IMPLEMENTS", "base.py#Base"),
    ])
    out = graph_store.implementations_detailed("test/repo", ["base.py#Base"])
    assert out == [
        {"id": "impl.py#Other", "rel": "IMPLEMENTS"},
        {"id": "impl.py#Sub", "rel": "IMPLEMENTS"},
    ]
    # символ без наследников → пусто
    assert graph_store.implementations_detailed("test/repo", ["impl.py#Sub"]) == []


@pytest.mark.integration
def test_implementations_detailed_method_overrides(graph_store):
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes("test/repo", ["base.py#Base.run", "impl.py#Sub.run"])
    graph_store.upsert_edges("test/repo", [
        ("impl.py#Sub.run", "IMPLEMENTS", "base.py#Base.run"),
    ])
    out = graph_store.implementations_detailed("test/repo", ["base.py#Base.run"])
    assert out == [{"id": "impl.py#Sub.run", "rel": "IMPLEMENTS"}]
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Требуется поднятый Neo4j (`docker compose up -d`).

Run: `.venv/bin/pytest tests/graph/test_store.py::test_implementations_detailed_returns_rel_and_id -m integration -v`
Expected: FAIL — `AttributeError: 'GraphStore' object has no attribute 'implementations_detailed'`.

- [ ] **Step 3: Реализовать метод**

В `reviewer/graph/store.py`, сразу после метода `callers_detailed` (после строки 106):

```python
    def implementations_detailed(self, repo: str, node_ids: list[str], *,
                                 branch: str = "") -> list[dict]:
        """Реализации/наследники символов — направленные входящие IMPLEMENTS.
        Класс → его подклассы; метод → его override-ы (SCIP эмитит и то, и то).
        Элементы: {"id": <node_id>, "rel": "IMPLEMENTS"}, упорядочены по id.
        Точны после полного `reviewer index` с SCIP (см. инвариант графа)."""
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid "
            "MATCH (c:Symbol {repo: $repo, branch: $branch})-[:IMPLEMENTS]->"
            "(s:Symbol {repo: $repo, branch: $branch, id: sid}) "
            "RETURN DISTINCT c.id AS id ORDER BY id",
            ids=list(node_ids), repo=repo, branch=branch)
        return [{"id": r["id"], "rel": "IMPLEMENTS"} for r in records]
```

- [ ] **Step 4: Прогнать — убедиться, что проходит**

Run: `.venv/bin/pytest tests/graph/test_store.py::test_implementations_detailed_returns_rel_and_id tests/graph/test_store.py::test_implementations_detailed_method_overrides -m integration -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/graph/store.py tests/graph/test_store.py
git add reviewer/graph/store.py tests/graph/test_store.py
git commit -m "feat(graph): implementations_detailed — входящие IMPLEMENTS в GraphStore (PRI-179)"
```

---

### Task 2: session-less `implementations` (service + регистрация в MCP-сервере)

**Files:**
- Modify: `reviewer/mcp/service.py` (метод после `callers`, ~строка 681)
- Modify: `reviewer/entrypoints/mcp_server.py` (регистрация `@mcp.tool()` после `callers`, ~строка 213)
- Test: `tests/mcp/test_service.py` (мок `_components` ~строка 63 + 3 теста после `test_callers_delegates_to_graph`, ~строка 651)
- Test: `tests/mcp/test_server_tools.py` (расширить `test_graph_base_tools_registered` ~строка 122 + новый forward-тест после `test_callers_tool_forwards`, ~строка 144)

**Interfaces:**
- Consumes: `GraphStore.implementations_detailed(...)` (Task 1); `MCPReviewService._resolve_repo_branch`, `._resolve_context_limits`, `format_neighbors`, `self.components.graph`, `self.components.store` (существуют); `create_server(service)` (существует).
- Produces: `MCPReviewService.implementations(repo: str, node_id: str, branch: str | None = None) -> str`; MCP-тул `implementations(repo, node_id, branch=None)`, форвардит в `service.implementations(repo, node_id, branch)`.

- [ ] **Step 1: Написать падающие unit-тесты (service)**

В `tests/mcp/test_service.py` в фабрике `_components()` (после строки 63 `c.graph.callers_detailed.return_value = []`) добавить:

```python
    c.graph.implementations_detailed.return_value = []
```

Затем после `test_callers_delegates_to_graph` (после строки 651) добавить:

```python
def test_implementations_delegates_to_graph() -> None:
    """implementations зовёт graph.implementations_detailed и форматирует компактно."""
    svc = _make_mcp_service()
    svc.components.graph.implementations_detailed.return_value = [
        {"id": "impl.py#Sub", "rel": "IMPLEMENTS"}]
    svc.components.store.fetch_nodes.return_value = [
        SimpleNamespace(node_id="impl.py#Sub", path="impl.py", start_line=3,
                        end_line=4, text="class Sub(Base): ...")]
    out = svc.implementations("a/b", "base.py#Base")
    assert "// impl.py#Sub (impl.py:3) [IMPLEMENTS]" in out
    svc.components.graph.implementations_detailed.assert_called_once_with(
        "a/b", ["base.py#Base"], branch=svc.settings.primary_branch())
    svc.components.graph.implementations_detailed.return_value = []
    assert svc.implementations("a/b", "base.py#Base") == "(implementations не найдены)"


def test_implementations_empty_and_failsoft() -> None:
    """Пусто → заглушка; исключение графа → тоже заглушка, не падаем."""
    svc = _make_mcp_service()
    svc.components.graph.implementations_detailed.return_value = []
    assert svc.implementations("a/b", "base.py#Base") == "(implementations не найдены)"
    svc.components.graph.implementations_detailed.side_effect = RuntimeError("neo4j down")
    assert svc.implementations("a/b", "base.py#Base") == "(implementations не найдены)"


def test_implementations_graph_none() -> None:
    """graph=None (Neo4j выключен) → '(граф недоступен)'."""
    svc = _make_mcp_service()
    svc.components.graph = None
    assert svc.implementations("a/b", "base.py#Base") == "(граф недоступен)"
```

- [ ] **Step 2: Написать падающие unit-тесты (регистрация в сервере)**

В `tests/mcp/test_server_tools.py`:

(a) в `test_graph_base_tools_registered` (строки 116-122) добавить строку установки мока и расширить множество:

```python
    svc.related_symbols.return_value = "x"
    svc.callers.return_value = "x"
    svc.definition.return_value = "x"
    svc.implementations.return_value = "x"
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"related_symbols", "callers", "definition", "implementations"} <= names
```

(b) после `test_callers_tool_forwards` (после строки 144) добавить:

```python
def test_implementations_tool_forwards():
    import asyncio

    svc = _service()
    svc.implementations.return_value = "impls"
    server = create_server(svc)
    asyncio.run(server.call_tool(
        "implementations", {"repo": "owner/name", "node_id": "base.py#Base"}))
    svc.implementations.assert_called_once_with("owner/name", "base.py#Base", None)
```

- [ ] **Step 3: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_service.py -k implementations tests/mcp/test_server_tools.py -k "implementations or graph_base" -q`
Expected: FAIL — `AttributeError: 'MCPReviewService' object has no attribute 'implementations'` / тул `implementations` не зарегистрирован.

- [ ] **Step 4: Реализовать метод сервиса**

В `reviewer/mcp/service.py`, сразу после метода `callers` (после строки 681):

```python
    def implementations(self, repo: str, node_id: str,
                        branch: str | None = None) -> str:
        """Кто реализует/наследует символ node_id ('path#fqn') — входящие
        IMPLEMENTS, без PR-сессии. Класс → подклассы; метод → override-ы.
        На элемент: file:line + строка определения + [IMPLEMENTS].
        Точны после полного `reviewer index` с SCIP."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        if self.components.graph is None:
            return "(граф недоступен)"
        cl = self._resolve_context_limits(repo, resolved)
        try:
            found = self.components.graph.implementations_detailed(
                repo, [node_id], branch=resolved)
        except Exception:
            log.warning("implementations: сбой графа", exc_info=True)
            return "(implementations не найдены)"
        return format_neighbors(
            found, store=self.components.store, repo=repo, branch=resolved,
            overlay_ref=None, changed_paths=[], empty_msg="(implementations не найдены)",
            cap=cl.graph.callers_topk)
```

- [ ] **Step 5: Зарегистрировать MCP-тул**

В `reviewer/entrypoints/mcp_server.py`, сразу после блока регистрации `callers` (после строки 213, перед `definition`):

```python
    @mcp.tool()
    def implementations(repo: str, node_id: str, branch: str | None = None) -> str:
        """Implementers/subclasses of a symbol node_id 'path#fqn' over the base
        index (incoming IMPLEMENTS, no PR session). A class node -> its subclasses;
        a method node -> its overrides. Each item: node_id + (file:line) + one-line
        definition snippet + [IMPLEMENTS]. Accurate after a full `reviewer index`
        with SCIP. branch defaults to the primary tracked branch."""
        return service.implementations(repo, node_id, branch)
```

- [ ] **Step 6: Прогнать — убедиться, что проходит**

Run: `.venv/bin/pytest tests/mcp/test_service.py tests/mcp/test_server_tools.py -q`
Expected: PASS (все тесты обоих файлов зелёные).

- [ ] **Step 7: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_service.py tests/mcp/test_server_tools.py
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_service.py tests/mcp/test_server_tools.py
git commit -m "feat(mcp): session-less тул implementations — directed IMPLEMENTS (PRI-179)"
```

---

### Task 3: prompt-поверхность solve-task + guard + пересборка манифестов

**Files:**
- Modify: `plugin/skills/_common/tool-usage.md` (секция «Session-less tools», ~после строки с `definition`)
- Modify: `plugin/skills/solve-task/SKILL.md` (блок «Deepen via the code graph», ~строка 164-170)
- Test: `tests/skills/test_solve_task_brief.py` (добавить guard-тест)
- Modify (сгенерировано): манифесты Codex-плагина через `scripts/update_codex_plugin_manifest.py`

**Interfaces:**
- Consumes: MCP-тул `implementations` (Task 2); include-механизм `<!-- include: _common/tool-usage.md -->` (solve-task SKILL.md строка 184).
- Produces: строка `implementations` в перечне session-less тулов промпта; hint в solve-task для OO/registry-задач; синхронные манифесты (payload-digest).

- [ ] **Step 1: Написать падающий guard-тест**

В `tests/skills/test_solve_task_brief.py` добавить:

```python
def test_solve_task_hints_implementations_for_oo():
    """OO/registry-хинт: directed implementations для наследников/реализаций."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "implementations" in text          # тул назван в шаге graph-deepening
    assert "IMPLEMENTS" in text or "наслед" in text  # смысл directed-обхода
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py::test_solve_task_hints_implementations_for_oo -q`
Expected: FAIL (`assert "implementations" in text`).

- [ ] **Step 3: Добавить тул в общий перечень session-less тулов**

В `plugin/skills/_common/tool-usage.md`, в секции `## Session-less tools`, сразу после строки `` - `definition(repo, symbol, branch?)` — where a symbol is defined.`` добавить:

```markdown
- `implementations(repo, node_id, branch?)` — directed subclasses/overrides (incoming IMPLEMENTS).
```

- [ ] **Step 4: Добавить OO/registry-хинт в solve-task**

В `plugin/skills/solve-task/SKILL.md`, в конце блока «Deepen via the code graph» (после строки 170, перед строкой `Pass the same branch you pass to search_codebase.`) добавить абзац:

```markdown
     For OO/registry/dispatch tasks («add a new provider / handler»), prefer directed
     `implementations(node_id)` (incoming IMPLEMENTS — who subclasses/overrides X) over the
     undirected `related_symbols`, which mixes callers/tests/implements. A class node → its
     subclasses; a method node → its overrides. Accurate after a full `reviewer index` with SCIP;
     fail-soft `(implementations не найдены)` is non-fatal — continue.
```

- [ ] **Step 5: Прогнать guard — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py -q`
Expected: PASS.

- [ ] **Step 6: Пересобрать манифесты Codex-плагина**

Правка контента под `plugin/` меняет payload-digest → синхронизируем манифесты.

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py`
Expected: `Codex plugin manifests synchronized`.

- [ ] **Step 7: Проверить install/skills-тесты**

Run: `.venv/bin/pytest tests/install -q && .venv/bin/python scripts/update_codex_plugin_manifest.py --check`
Expected: PASS; `--check` не печатает ошибок (код 0).

- [ ] **Step 8: Линт + коммит**

```bash
.venv/bin/ruff check tests/skills/test_solve_task_brief.py
git add plugin/skills/_common/tool-usage.md plugin/skills/solve-task/SKILL.md tests/skills/test_solve_task_brief.py
git add -A -- ':(glob)**/.codex-plugin*' 2>/dev/null || true
git add -A plugin/ .claude-plugin/ 2>/dev/null || true
git status --short   # убедиться, что застейджены только манифесты + плагин + тест
git commit -m "feat(skill): хинт implementations для OO-задач solve-task + манифесты (PRI-179)"
```

> Примечание: если `git status` в предыдущем шаге покажет неожиданные файлы вне `plugin/`, `.claude-plugin/` и теста — застейджить их вручную адресно, не через `-A`.

---

### Task 4: документация (оба README + CLAUDE.md)

**Files:**
- Modify: `README.md:594` (session-less priming-блок)
- Modify: `README.ru.md:94` (таблица модуля `reviewer/tools`) и `README.ru.md:604` (перечень MCP-тулов solve-task)
- Modify: `CLAUDE.md:99` (таблица модуля `reviewer/tools`)

**Interfaces:**
- Consumes: тул `implementations` (Task 2). Produces: доки, отражающие новый session-less тул (инвариант «оба README синхронны»).

- [ ] **Step 1: README.md — priming-блок**

Строка 594-595: в перечне `` `search_codebase` (relevant code), `callers` (…), `related_symbols`, `definition`.`` дописать `implementations`, например заменить `` `related_symbols`, `definition`.`` на:

```markdown
> `related_symbols`, `definition`, `implementations` (directed subclasses/overrides).
```

- [ ] **Step 2: README.ru.md — таблица модуля (строка 94)**

В ячейке `session-less ... для Q&A` заменить `` `search_codebase`/`related_symbols`/`callers`/`definition` `` на:

```markdown
`search_codebase`/`related_symbols`/`callers`/`definition`/`implementations`
```

- [ ] **Step 3: README.ru.md — перечень тулов solve-task (строка 604)**

Заменить `` `search_codebase`, `related_symbols`, `callers`, `definition`, `get_pr_diff`; `` на:

```markdown
  `search_codebase`, `related_symbols`, `callers`, `definition`, `implementations`, `get_pr_diff`; плюс подключённая
```

- [ ] **Step 4: CLAUDE.md — таблица модуля (строка 99)**

В ячейке `reviewer/tools` заменить `session-less варианты для Q&A — `search_codebase`/`related_symbols`/`callers`/`definition` в `mcp/service.py`` на:

```markdown
session-less варианты для Q&A — `search_codebase`/`related_symbols`/`callers`/`definition`/`implementations` в `mcp/service.py`
```

- [ ] **Step 5: Проверить, что доки не сломали grounding-тесты**

Run: `.venv/bin/pytest tests/skills/test_readme_grounding_block.py tests/test_review_yml_example.py -q`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add README.md README.ru.md CLAUDE.md
git commit -m "docs: тул implementations в обоих README и CLAUDE.md (PRI-179)"
```

---

### Финальная проверка (после всех задач)

- [ ] **Полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены по умолчанию).

- [ ] **Integration store-тест (если Neo4j поднят)**

Run: `docker compose up -d && .venv/bin/pytest tests/graph/test_store.py -m integration -q`
Expected: PASS (включая 2 новых теста `implementations_detailed`).

- [ ] **Линт всего дерева правок**

Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: без новых ошибок (не гнаться за repo-wide clean — только затронутые файлы).

---

## Self-Review (выполнено автором плана)

**1. Покрытие спеки:**
- store `implementations_detailed` → Task 1 ✅
- service `implementations` → Task 2 (Steps 4) ✅
- регистрация `@mcp.tool()` → Task 2 (Step 5) ✅
- hint в solve-task → Task 3 (Steps 3-4) ✅
- тесты store (позитив/override/пусто) → Task 1 ✅; service (delegate/failsoft/none) → Task 2 ✅; skills guard → Task 3 ✅; server registration/forward → Task 2 ✅
- НЕ трогаем graph_sync/builder/PR-session → зафиксировано в Global Constraints ✅
- доки README×2 + CLAUDE.md → Task 4 ✅
- манифесты → Task 3 (Steps 6-7) ✅

**2. Плейсхолдеры:** нет — весь код и команды приведены дословно.

**3. Согласованность типов:** `implementations_detailed(repo, node_ids, *, branch="") -> list[{"id","rel":"IMPLEMENTS"}]` (Task 1) ровно то, что зовёт `service.implementations` через `implementations_detailed(repo, [node_id], branch=resolved)` (Task 2); `format_neighbors` принимает тот же контракт, что у `callers`. MCP-тул `implementations(repo, node_id, branch=None)` форвардит в `service.implementations(repo, node_id, branch)` — сигнатуры совпадают с тестом-форвардером.
