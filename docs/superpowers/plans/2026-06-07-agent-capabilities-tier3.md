# Расширение возможностей агента ревью (Тир 3) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать агенту ревью больше возможностей — точные инструменты работы с кодом, контекст PR целиком, агентную верификацию находок и кросс-файловый синтез.

**Architecture:** Расширяем `ToolContext` четырьмя инструментами и добавляем направленные методы графа; прокидываем интент PR в анализ; заменяем one-shot верификатор на агентный (за флагом) и вставляем узел `synthesize` между `verify` и `assemble` (за флагом). Внешние сервисы — за прежними интерфейсами, новые ветки откатываются флагами.

**Tech Stack:** Python 3.11+, LangGraph, LangChain core, Neo4j (граф), ParadeDB/pgvector (индекс), pytest, ruff.

**Спек:** `docs/superpowers/specs/2026-06-07-agent-capabilities-tier3-design.md`

**Замечания по стилю проекта:**
- Язык — русский: докстринги, сообщения, промпты.
- Unit-тесты на фейках, внешние API не дёргают; реальные граф/стор помечаются `@pytest.mark.integration`.
- Команды: `.venv/bin/pytest -q`, один файл/тест — `.venv/bin/pytest tests/.../file.py::name -q`.
- Коммиты: Conventional Commits на русском, **без** self-attribution.
- Линт: `.venv/bin/ruff check .` (line-length 100).

---

### Task 1: Направленные методы графа — `GraphStore.callers` и `find_symbol`

**Files:**
- Modify: `reviewer/graph/store.py` (после метода `expand`, ~строка 42)
- Test: `tests/graph/test_store.py` (добавить, integration)

- [ ] **Step 1: Написать падающие integration-тесты**

В конец `tests/graph/test_store.py` добавить:

```python
@pytest.mark.integration
def test_callers_directed():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema(); g.clear()
    g.upsert_nodes(["a.py#f", "a.py#g", "a.py#h"])
    # g вызывает f; h вызывает f
    g.upsert_edges([("a.py#g", "CALLS", "a.py#f"), ("a.py#h", "CALLS", "a.py#f")])
    callers = g.callers(["a.py#f"])
    assert callers == {"a.py#g", "a.py#h"}
    assert g.callers(["a.py#g"]) == set()   # g никто не вызывает
    g.close()


@pytest.mark.integration
def test_find_symbol_prefers_exact_suffix():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema(); g.clear()
    g.upsert_nodes(["a.py#run", "b.py#A.run", "c.py#runner"])
    ids = g.find_symbol("run")
    # точное имя (#run, A.run) раньше, чем подстрока (runner)
    assert ids[0] in {"a.py#run", "b.py#A.run"}
    assert "a.py#run" in ids and "b.py#A.run" in ids
    g.close()
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `.venv/bin/pytest -m integration tests/graph/test_store.py -q`
Expected: FAIL — `AttributeError: 'GraphStore' object has no attribute 'callers'` (нужны поднятые Neo4j; если нет — тесты не соберутся по отсутствию метода).

- [ ] **Step 3: Реализовать методы**

В `reviewer/graph/store.py` после `expand` (строка 42) добавить:

```python
    def callers(self, node_ids: list[str]) -> set[str]:
        """Кто вызывает данные символы — направленные входящие CALLS."""
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid MATCH (c:Symbol)-[:CALLS]->(s:Symbol {id: sid}) "
            "RETURN DISTINCT c.id AS id",
            ids=list(node_ids))
        return {r["id"] for r in records}

    def find_symbol(self, name: str) -> list[str]:
        """Резолв имени символа в node_id ('path#fqn'). Точное имя (#name / .name)
        приоритетнее подстроки. Возврат — до 25 id, точные сперва."""
        records, _, _ = self._driver.execute_query(
            "MATCH (s:Symbol) WHERE s.id CONTAINS $needle "
            "RETURN s.id AS id LIMIT 50",
            needle=name)
        ids = [r["id"] for r in records]
        suffix = "#" + name
        exact = [i for i in ids if i.endswith(suffix) or i.endswith("." + name)]
        rest = [i for i in ids if i not in exact]
        return (exact + rest)[:25]
```

- [ ] **Step 4: Запустить — убедиться, что проходят**

Run: `.venv/bin/pytest -m integration tests/graph/test_store.py -q`
Expected: PASS (при поднятом Neo4j).

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/graph/store.py
git add reviewer/graph/store.py tests/graph/test_store.py
git commit -m "feat(graph): направленные callers и резолв find_symbol в GraphStore"
```

---

### Task 2: Флаги настроек

**Files:**
- Modify: `reviewer/config/settings.py` (блок «review tuning», ~строка 22)
- Test: `tests/config/test_settings_flags.py` (создать)

- [ ] **Step 1: Написать падающий тест**

Создать `tests/config/test_settings_flags.py`:

```python
from reviewer.config.settings import Settings


def test_tier3_flags_have_defaults():
    s = Settings()
    assert s.review_agentic_verify is True
    assert s.review_synthesis is True
    assert s.review_verify_min_severity == "medium"
    assert s.review_verify_max_iterations == 3
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/config/test_settings_flags.py -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'review_agentic_verify'`.

- [ ] **Step 3: Добавить поля**

В `reviewer/config/settings.py` в блок «review tuning» (после `review_suggestions`, строка 28) добавить:

```python
    review_agentic_verify: bool = True            # агентная поштучная верификация находок
    review_synthesis: bool = True                 # кросс-файловый узел synthesize
    review_verify_min_severity: str = "medium"    # порог severity для агентной проверки
    review_verify_max_iterations: int = 3         # бюджет tool-loop верификатора на находку
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/config/test_settings_flags.py -q`
Expected: PASS.

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/config/settings.py
git add reviewer/config/settings.py tests/config/test_settings_flags.py
git commit -m "feat(config): флаги Тира 3 (agentic verify, synthesis, пороги)"
```

---

### Task 3: Обогащённый `ToolContext` + 4 новых инструмента

**Files:**
- Modify: `reviewer/tools/code_tools.py` (целиком — `ToolContext` + `make_tools`)
- Test: `tests/tools/test_code_tools.py` (добавить тесты)

- [ ] **Step 1: Написать падающие тесты новых инструментов**

В `tests/tools/test_code_tools.py` добавить (рядом с существующими фейками):

```python
class FakeGraphRich:
    def expand(self, ids, hops=2): return {"b.py#g"}
    def callers(self, ids): return {"x.py#caller"}
    def find_symbol(self, name): return ["a.py#f"]

class FakeStore:
    def fetch_nodes(self, ids, overlay_ref, changed_paths):
        from reviewer.index.store import Retrieved
        return [Retrieved("a.py#f", "a.py", "f", "function", 1, 2, "def f():\n    return 1", 0.0)]


def _rich_ctx(**over):
    base = dict(retriever=FakeRetriever(), graph=FakeGraphRich(),
                overlay_ref="pr:1", changed_paths=["a.py"], changed_node_ids=[],
                read_file_fn=lambda p: "l1\nl2\nl3" if p == "a.py" else None,
                patches={"a.py": "@@ -1 +1 @@\n-x\n+y"}, store=FakeStore())
    base.update(over)
    return ToolContext(**base)


def test_read_file_returns_numbered_slice():
    tools = {t.name: t for t in make_tools(_rich_ctx())}
    out = tools["read_file"].invoke({"path": "a.py", "start": 1, "end": 2})
    assert "1|l1" in out and "2|l2" in out and "3|l3" not in out


def test_read_file_missing():
    tools = {t.name: t for t in make_tools(_rich_ctx())}
    out = tools["read_file"].invoke({"path": "nope.py"})
    assert "не найден" in out


def test_get_definition_uses_graph_and_store():
    tools = {t.name: t for t in make_tools(_rich_ctx())}
    out = tools["get_definition"].invoke({"symbol": "f"})
    assert "a.py#f" in out and "def f" in out


def test_find_callers_directed():
    tools = {t.name: t for t in make_tools(_rich_ctx())}
    out = tools["find_callers"].invoke({"node_id": "a.py#f"})
    assert "x.py#caller" in out


def test_get_changed_file_diff_returns_patch():
    tools = {t.name: t for t in make_tools(_rich_ctx())}
    out = tools["get_changed_file_diff"].invoke({"path": "a.py"})
    assert "+y" in out
    out2 = tools["get_changed_file_diff"].invoke({"path": "other.py"})
    assert "не входит" in out2
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `.venv/bin/pytest tests/tools/test_code_tools.py -q`
Expected: FAIL — `TypeError` на неизвестных полях `ToolContext` / отсутствуют инструменты `read_file` и т.д.

- [ ] **Step 3: Переписать `reviewer/tools/code_tools.py`**

```python
from __future__ import annotations
from dataclasses import dataclass, field
from langchain_core.tools import StructuredTool

@dataclass
class ToolContext:
    retriever: object
    graph: object
    overlay_ref: str
    changed_paths: list[str]
    changed_node_ids: list[str] = field(default_factory=list)
    read_file_fn: object = None            # Callable[[str], str | None] — head-версия файла
    patches: dict = field(default_factory=dict)
    store: object = None                   # индекс-стор для get_definition

def make_tools(ctx: ToolContext) -> list[StructuredTool]:
    def search_code(query: str) -> str:
        """Семантико-лексический поиск релевантного кода по всему репозиторию."""
        pack = ctx.retriever.retrieve(
            query=query, changed_node_ids=ctx.changed_node_ids,
            overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths, top_k=8)
        return pack.as_context() or "(ничего не найдено)"

    def get_related_symbols(node_id: str) -> str:
        """Связанные символы (вызовы/реализации/тесты) для node_id вида 'path#fqn'."""
        related = ctx.graph.expand([node_id], hops=2)
        return "\n".join(sorted(related)) or "(нет связей)"

    def read_file(path: str, start: int = 1, end: int = 400) -> str:
        """Точный исходник файла на head-ревизии PR, строки [start..end] с номерами (N|код).
        Окно ограничено 400 строками."""
        if ctx.read_file_fn is None:
            return "(чтение файлов недоступно)"
        src = ctx.read_file_fn(path)
        if src is None:
            return f"(файл не найден: {path})"
        lines = src.splitlines()
        if not lines:
            return "(файл пуст)"
        s = max(1, start)
        e = min(len(lines), end)
        if e - s + 1 > 400:
            e = s + 399
        if s > len(lines):
            return f"(нет строки {s}; в файле {len(lines)} строк)"
        body = "\n".join(f"{i}|{lines[i - 1]}" for i in range(s, e + 1))
        if e < len(lines):
            body += "\n(…усечено)"
        return body

    def get_definition(symbol: str) -> str:
        """Где определён символ + его исходный код. Резолв имени через граф, код — через индекс.
        Фолбэк на семантический поиск, если граф/стор пусты."""
        ids: list[str] = []
        if ctx.graph is not None and hasattr(ctx.graph, "find_symbol"):
            ids = ctx.graph.find_symbol(symbol)
        if ids and ctx.store is not None:
            nodes = ctx.store.fetch_nodes(ids[:3], ctx.overlay_ref, ctx.changed_paths)
            if nodes:
                return "\n\n".join(
                    f"// {n.node_id} ({n.path}:{n.start_line}-{n.end_line})\n{n.text}"
                    for n in nodes)
        pack = ctx.retriever.retrieve(
            query=symbol, changed_node_ids=ctx.changed_node_ids,
            overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths, top_k=3)
        return pack.as_context() or "(определение не найдено)"

    def find_callers(node_id: str) -> str:
        """Кто вызывает символ node_id ('path#fqn') — направленный CALLS (impact-анализ)."""
        if ctx.graph is None or not hasattr(ctx.graph, "callers"):
            return "(граф недоступен)"
        found = ctx.graph.callers([node_id])
        return "\n".join(sorted(found)) or "(вызовов не найдено)"

    def get_changed_file_diff(path: str) -> str:
        """Дифф другого изменённого файла этого PR."""
        patch = (ctx.patches or {}).get(path)
        return patch or "(файл не входит в изменения PR)"

    return [
        StructuredTool.from_function(search_code),
        StructuredTool.from_function(get_related_symbols),
        StructuredTool.from_function(read_file),
        StructuredTool.from_function(get_definition),
        StructuredTool.from_function(find_callers),
        StructuredTool.from_function(get_changed_file_diff),
    ]
```

- [ ] **Step 4: Запустить — убедиться, что проходят (включая старые тесты файла)**

Run: `.venv/bin/pytest tests/tools/test_code_tools.py -q`
Expected: PASS (новые + два прежних теста).

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/tools/code_tools.py
git add reviewer/tools/code_tools.py tests/tools/test_code_tools.py
git commit -m "feat(tools): read_file/get_definition/find_callers/get_changed_file_diff + обогащённый ToolContext"
```

---

### Task 4: Контекст PR в анализе + общий конвертер находок

**Files:**
- Modify: `reviewer/agent/state.py` (поля `Deps`)
- Modify: `reviewer/agent/analyzer.py` (`_FindingModel.file`, конвертер `_to_findings`, PR-контекст, построение `ToolContext`)
- Modify: `reviewer/agent/prompts.py` (`ANALYZE_SYSTEM`)
- Modify: `reviewer/entrypoints/cli.py` (проброс `pr_title`/`pr_body`/`changed_status`)
- Test: `tests/agent/test_analyzer.py` (добавить тесты)

- [ ] **Step 1: Написать падающие тесты**

В `tests/agent/test_analyzer.py` добавить. Обнови `_deps()` там же, добавив новые поля (с дефолтами они не обязательны, но тест PR-контекста их использует):

```python
from reviewer.agent.analyzer import _pr_context, _to_findings, _FindingModel


def test_pr_context_includes_title_and_manifest():
    deps = _deps()
    deps.pr_title = "Fix auth"
    deps.pr_body = "body text"
    deps.changed_status = {"a.py": "modified"}
    out = _pr_context(deps, ["a.py"])
    assert "Fix auth" in out and "body text" in out and "a.py (modified)" in out


def test_to_findings_respects_model_file_then_default():
    models = [
        _FindingModel(category="correctness", severity="high", message="m1", file="other.py"),
        _FindingModel(category="security", severity="low", message="m2"),
    ]
    out = _to_findings(models, default_file="a.py")
    assert out[0].file == "other.py"     # из модели
    assert out[1].file == "a.py"         # фолбэк на default
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -q`
Expected: FAIL — `ImportError`/`AttributeError` (`_pr_context`, `_to_findings`, `file` у `_FindingModel`, поля `Deps`).

- [ ] **Step 3a: Поля `Deps` — `reviewer/agent/state.py`**

В dataclass `Deps` после `suggestions_mode` добавить:

```python
    pr_title: str = ""
    pr_body: str = ""
    changed_status: dict | None = None
    synthesizer: object = None        # LLMSynthesizer (Task 6); None = узел выключен
```

- [ ] **Step 3b: `_FindingModel.file` + конвертер + PR-контекст — `reviewer/agent/analyzer.py`**

В `_FindingModel` добавить поле (после `category`):

```python
    file: str | None = None
```

Добавить свободные функции (например, после `_extract_json`, до классов моделей `_to_findings` поставить ниже моделей — порядок: сначала модели, потом конвертер). Конкретно: после класса `_Findings` добавить:

```python
def _to_findings(models, default_file: str) -> list[Finding]:
    """Преобразовать распарсенные модели в Finding. file берётся из модели либо default."""
    out: list[Finding] = []
    for f in models:
        fs = f.fix.start_line if f.fix else None
        fe = f.fix.end_line if f.fix else None
        rp = f.fix.replacement if f.fix else None
        if rp is not None and (fs is None or fe is None):
            rp = None
        out.append(Finding(
            category=f.category,
            severity=(f.severity if f.severity in _VALID_SEVERITY else "medium"),
            file=(f.file or default_file), line=f.line, side="RIGHT", message=f.message,
            suggestion=f.suggestion, confidence=f.confidence,
            fix_start=fs, fix_end=fe, replacement=rp))
    return out


def _pr_context(deps, changed_paths: list[str]) -> str:
    """Префикс human-промпта: интент PR + манифест изменённых файлов."""
    parts: list[str] = []
    if getattr(deps, "pr_title", ""):
        parts.append(f"Заголовок PR: {deps.pr_title}")
    if getattr(deps, "pr_body", ""):
        parts.append(f"Описание PR: {deps.pr_body[:1500]}")
    status = getattr(deps, "changed_status", None) or {}
    manifest = "\n".join(f"  - {p} ({status.get(p, 'modified')})" for p in changed_paths)
    if manifest:
        parts.append("Изменённые файлы PR:\n" + manifest)
    return "\n".join(parts)
```

В `LLMAnalyzer.analyze` заменить тело сборки `out` циклом-конвертером и добавить PR-контекст + новые поля `ToolContext`:

```python
    def analyze(self, unit: ReviewUnit, deps: Deps) -> list[Finding]:
        ctx = ToolContext(
            retriever=deps.retriever, graph=deps.graph,
            overlay_ref=deps.overlay_ref, changed_paths=deps.changed_paths,
            changed_node_ids=unit.node_ids,
            read_file_fn=((lambda p: deps.vcs.get_file_at_ref(p, deps.head_sha))
                          if deps.vcs else None),
            patches=deps.patches, store=getattr(deps.retriever, "store", None))
        tools = make_tools(ctx)
        llm = self.provider.chat_model_with_tools(tools)
        tools_by_name = {t.name: t for t in tools}
        budget = BudgetTracker(self.max_iterations)
        numbered = _numbered(unit.new_source)
        pr_ctx = _pr_context(deps, deps.changed_paths)
        human = (pr_ctx + "\n\n") if pr_ctx else ""
        human += f"Файл: {unit.path}\n"
        if numbered:
            human += f"Новая версия файла (с номерами строк N|код):\n{numbered}\n\n"
        human += f"Изменения (дифф):\n{unit.changed_text}"
        messages = [SystemMessage(ANALYZE_SYSTEM), HumanMessage(human)]
        try:
            while True:
                budget.tick()
                ai = llm.invoke(messages)
                messages.append(ai)
                if not ai.tool_calls:
                    break
                for call in ai.tool_calls:
                    tool = tools_by_name.get(call["name"])
                    try:
                        if tool is None:
                            result = f"(неизвестный инструмент: {call['name']})"
                        else:
                            result = tool.invoke(call["args"])
                    except Exception as e:
                        result = f"(ошибка инструмента {call['name']}: {e})"
                    messages.append(ToolMessage(str(result), tool_call_id=call["id"]))
        except BudgetExceeded:
            pass
        resp = self.provider.chat_model().invoke(messages + [HumanMessage(_FINDINGS_SCHEMA)])
        data = _extract_json(_text_of(resp))
        try:
            parsed = _Findings(**data)
        except Exception:
            parsed = _Findings()
        return _to_findings(parsed.findings, default_file=unit.path)
```

- [ ] **Step 3c: `ANALYZE_SYSTEM` — `reviewer/agent/prompts.py`**

Заменить `ANALYZE_SYSTEM` на:

```python
ANALYZE_SYSTEM = """Ты — старший ревьюер. Анализируй ТОЛЬКО изменения данного файла \
в контексте всего PR. Тебе доступны инструменты: search_code (поиск кода), \
get_related_symbols/find_callers (связи и вызывающие), get_definition (определение символа), \
read_file (точный исходник любого файла на новой версии), get_changed_file_diff (дифф \
другого изменённого файла PR). Прежде чем делать вывод, проверяй влияние изменения: \
если меняется сигнатура/контракт функции — через find_callers найди вызовы и через \
read_file/get_changed_file_diff проверь, согласованы ли они. Учитывай заявленный интент PR. \
Сообщай только реальные проблемы: баги, edge-cases, безопасность, нарушенные контракты, \
кросс-файловые рассогласования. Не комментируй стиль, если не просили. Для каждой проблемы \
укажи файл, строку (по НОВОЙ версии), severity, краткое сообщение и, по возможности, suggestion."""
```

- [ ] **Step 3d: Проброс в CLI — `reviewer/entrypoints/cli.py`**

В функции `review`, где собирается `deps = Deps(...)` (строки 83–88), добавить поля. Перед сборкой `units` уже есть `files`; собрать статусы:

Заменить блок создания `deps` на:

```python
        changed_status = {f.path: f.status for f in files}
        deps = Deps(vcs=vcs, retriever=c.retriever, graph=c.graph, policy=policy,
                    analyzer=LLMAnalyzer(c.llm_provider, s.review_max_tool_iterations),
                    verifier=LLMVerifier(c.llm_provider), pr_number=pr,
                    head_sha=prq.head_sha, overlay_ref=f"pr:{pr}",
                    changed_paths=changed, patches={f.path: f.patch for f in files},
                    suggestions_mode=s.review_suggestions,
                    pr_title=prq.title, pr_body=prq.body, changed_status=changed_status)
```

(Поля `verifier`/`synthesizer` и топология графа дорабатываются в Task 5–6; пока оставляем как есть — `LLMVerifier(c.llm_provider)` с дефолтами обратносовместим.)

- [ ] **Step 4: Запустить — убедиться, что проходят (новые + прежние тесты анализатора)**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -q`
Expected: PASS.

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/agent/analyzer.py reviewer/agent/state.py reviewer/agent/prompts.py reviewer/entrypoints/cli.py
git add reviewer/agent/analyzer.py reviewer/agent/state.py reviewer/agent/prompts.py reviewer/entrypoints/cli.py tests/agent/test_analyzer.py
git commit -m "feat(agent): контекст PR (интент+манифест) в анализе + общий конвертер находок"
```

---

### Task 5: Агентный верификатор

**Files:**
- Modify: `reviewer/agent/analyzer.py` (`LLMVerifier` — режим agentic)
- Modify: `reviewer/agent/prompts.py` (`VERIFY_SYSTEM`)
- Modify: `reviewer/entrypoints/cli.py` (конструирование `LLMVerifier` с флагами)
- Test: `tests/agent/test_analyzer.py` (добавить тесты)

- [ ] **Step 1: Написать падающие тесты**

В `tests/agent/test_analyzer.py` добавить:

```python
def _finding(severity="high", confidence=0.9, msg="bug"):
    return Finding(category="correctness", severity=severity, file="a.py", line=2,
                   side="RIGHT", message=msg, suggestion=None, confidence=confidence)


def test_agentic_verify_low_severity_passes_without_llm():
    # severity ниже порога и confidence высокий -> проходит без обращения к LLM
    class BoomProvider:
        def chat_model_with_tools(self, tools): raise AssertionError("не должно вызываться")
        def chat_model(self): raise AssertionError("не должно вызываться")
    v = LLMVerifier(BoomProvider(), agentic=True, max_iterations=2, min_severity="high")
    out = v.verify([_finding(severity="low", confidence=0.9)], _deps())
    assert len(out) == 1


def test_agentic_verify_drops_false_positive():
    final = '{"is_real": false}'
    prov = FakeProvider([AIMessage(content="done")], final)   # без tool_calls -> сразу вердикт
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="low")
    out = v.verify([_finding(severity="high")], _deps())
    assert out == []


def test_agentic_verify_fail_open_on_unparseable():
    prov = FakeProvider([AIMessage(content="done")], "мусор без json")
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="low")
    out = v.verify([_finding(severity="high")], _deps())
    assert len(out) == 1   # не разобрали вердикт -> оставляем


def test_oneshot_verify_still_works_when_not_agentic():
    prov = FakeProvider([], '{"verdicts":[{"index":0,"is_real":false}]}')
    v = LLMVerifier(prov, agentic=False)
    out = v.verify([_finding(severity="high")], _deps())
    assert out == []
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -k verify -q`
Expected: FAIL — `LLMVerifier.__init__` не принимает `agentic`/`max_iterations`/`min_severity`.

- [ ] **Step 3a: Переписать `LLMVerifier` — `reviewer/agent/analyzer.py`**

Добавить константу порядка severity рядом с `_VALID_SEVERITY` (строка 13):

```python
_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_VERDICT_ONE_SCHEMA = 'Верни СТРОГО один JSON-объект: {"is_real": true|false}'
```

Заменить класс `LLMVerifier` целиком на:

```python
class LLMVerifier:
    """Верификатор находок. agentic=True — поштучная проверка с инструментами;
    agentic=False — прежний one-shot список (обратносовместимо)."""
    def __init__(self, llm_provider, agentic: bool = False,
                 max_iterations: int = 3, min_severity: str = "medium"):
        self.provider = llm_provider
        self.agentic = agentic
        self.max_iterations = max_iterations
        self.min_severity = min_severity

    def verify(self, findings: list[Finding], deps: Deps) -> list[Finding]:
        if not findings:
            return []
        if not self.agentic:
            return self._verify_oneshot(findings, deps)
        return [f for f in findings if self._verify_one(f, deps)]

    def _needs_check(self, f: Finding) -> bool:
        sev_ok = (_SEVERITY_ORDER.get(f.severity, 1)
                  >= _SEVERITY_ORDER.get(self.min_severity, 1))
        return sev_ok or f.confidence < 0.5

    def _verify_one(self, f: Finding, deps: Deps) -> bool:
        if not self._needs_check(f):
            return True   # дёшево пропускаем (не теряем находку)
        ctx = ToolContext(
            retriever=deps.retriever, graph=deps.graph,
            overlay_ref=deps.overlay_ref, changed_paths=deps.changed_paths,
            changed_node_ids=[],
            read_file_fn=((lambda p: deps.vcs.get_file_at_ref(p, deps.head_sha))
                          if deps.vcs else None),
            patches=deps.patches, store=getattr(deps.retriever, "store", None))
        tools = make_tools(ctx)
        llm = self.provider.chat_model_with_tools(tools)
        tools_by_name = {t.name: t for t in tools}
        budget = BudgetTracker(self.max_iterations)
        human = (f"Замечание для проверки:\n[{f.category}/{f.severity}] "
                 f"{f.file}:{f.line} {f.message}\n\n"
                 "Проверь по реальному коду через инструменты (read_file, find_callers, "
                 "get_definition), затем верни вердикт.")
        messages = [SystemMessage(VERIFY_SYSTEM), HumanMessage(human)]
        try:
            while True:
                budget.tick()
                ai = llm.invoke(messages)
                messages.append(ai)
                if not ai.tool_calls:
                    break
                for call in ai.tool_calls:
                    tool = tools_by_name.get(call["name"])
                    try:
                        result = (tool.invoke(call["args"]) if tool
                                  else f"(неизвестный инструмент: {call['name']})")
                    except Exception as e:
                        result = f"(ошибка инструмента {call['name']}: {e})"
                    messages.append(ToolMessage(str(result), tool_call_id=call["id"]))
        except BudgetExceeded:
            pass
        resp = self.provider.chat_model().invoke(
            messages + [HumanMessage(_VERDICT_ONE_SCHEMA)])
        data = _extract_json(_text_of(resp))
        if "is_real" not in data:
            return True   # fail-open: не разобрали -> оставляем
        return bool(data.get("is_real", True))

    def _verify_oneshot(self, findings: list[Finding], deps: Deps) -> list[Finding]:
        listing = "\n".join(
            f"{i}. [{f.category}/{f.severity}] {f.file}:{f.line} {f.message}"
            for i, f in enumerate(findings))
        resp = self.provider.chat_model().invoke(
            [SystemMessage(VERIFY_SYSTEM), HumanMessage(listing + "\n\n" + _VERDICT_SCHEMA)])
        data = _extract_json(_text_of(resp))
        if "verdicts" not in data:
            return findings
        try:
            vb = _VerdictBatch(**data)
        except Exception:
            return findings
        verdict_by_idx = {v.index: v.is_real for v in vb.verdicts}
        return [f for i, f in enumerate(findings) if verdict_by_idx.get(i, True)]
```

- [ ] **Step 3b: `VERIFY_SYSTEM` — `reviewer/agent/prompts.py`**

Заменить `VERIFY_SYSTEM` на:

```python
VERIFY_SYSTEM = """Ты — придирчивый, но честный рецензент: отсеиваешь ТОЛЬКО ложные \
срабатывания, не теряя реальные баги. Используй инструменты (read_file, find_callers, \
get_definition, search_code), чтобы проверить факт по реальному коду, а не угадывать. \
Ставь is_real=false ТОЛЬКО если замечание явно неверно: галлюцинация, неправильное \
прочтение кода, придирка к стилю без эффекта, или проблема уже обработана рядом. \
Если замечание правдоподобно ИЛИ есть хоть какое-то сомнение — ставь is_real=true. \
Лучше оставить спорное замечание, чем потерять настоящий баг."""
```

- [ ] **Step 3c: Конструирование с флагами — `reviewer/entrypoints/cli.py`**

В `review` заменить `verifier=LLMVerifier(c.llm_provider)` на:

```python
                    verifier=LLMVerifier(c.llm_provider, agentic=s.review_agentic_verify,
                                         max_iterations=s.review_verify_max_iterations,
                                         min_severity=s.review_verify_min_severity),
```

- [ ] **Step 4: Запустить — убедиться, что проходят (включая прежний verify-тест)**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -q`
Expected: PASS.

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/agent/analyzer.py reviewer/agent/prompts.py reviewer/entrypoints/cli.py
git add reviewer/agent/analyzer.py reviewer/agent/prompts.py reviewer/entrypoints/cli.py tests/agent/test_analyzer.py
git commit -m "feat(agent): агентный поштучный верификатор с инструментами (за флагом, fail-open)"
```

---

### Task 6: Узел кросс-файлового синтеза + топология графа

**Files:**
- Modify: `reviewer/agent/analyzer.py` (`LLMSynthesizer`, `SYNTHESIZE_SYSTEM` импорт)
- Modify: `reviewer/agent/prompts.py` (`SYNTHESIZE_SYSTEM`)
- Modify: `reviewer/agent/nodes.py` (`make_synthesize_node`)
- Modify: `reviewer/agent/graph.py` (узел + топология за флагом)
- Modify: `reviewer/entrypoints/cli.py` (передать `synthesizer` + флаг в граф)
- Test: `tests/agent/test_analyzer.py` и `tests/agent/test_graph.py`

- [ ] **Step 1: Написать падающие тесты**

В `tests/agent/test_analyzer.py` добавить:

```python
from reviewer.agent.analyzer import LLMSynthesizer


def test_synthesize_replaces_with_parsed_findings():
    final = ('{"findings":[{"category":"correctness","severity":"high","line":5,'
             '"message":"caller mismatch","file":"b.py"}]}')
    prov = FakeProvider([AIMessage(content="done")], final)
    s = LLMSynthesizer(prov, max_iterations=2)
    out = s.synthesize([_finding(severity="high", msg="orig")], _deps())
    assert len(out) == 1 and out[0].file == "b.py" and out[0].message == "caller mismatch"


def test_synthesize_fail_open_keeps_input_on_empty_or_unparseable():
    prov = FakeProvider([AIMessage(content="done")], "не json")
    s = LLMSynthesizer(prov, max_iterations=2)
    inp = [_finding(severity="high", msg="keep me")]
    out = s.synthesize(inp, _deps())
    assert out == inp
```

В `tests/agent/test_graph.py` добавить (топология с/без synthesize):

```python
from reviewer.agent.graph import build_graph
from reviewer.agent.state import Deps


def _min_deps(**over):
    base = dict(vcs=None, retriever=None, graph=None, policy=None, analyzer=None,
                verifier=None, pr_number=1, head_sha="s", overlay_ref="pr:1",
                changed_paths=[], patches={})
    base.update(over)
    return Deps(**base)


def test_graph_includes_synthesize_when_synthesizer_present():
    g = build_graph(_min_deps(synthesizer=object()))
    assert "synthesize" in g.get_graph().nodes


def test_graph_skips_synthesize_when_no_synthesizer():
    g = build_graph(_min_deps(synthesizer=None))
    assert "synthesize" not in g.get_graph().nodes
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -k synthesize tests/agent/test_graph.py -q`
Expected: FAIL — нет `LLMSynthesizer`, граф не знает про `synthesize`.

- [ ] **Step 3a: `LLMSynthesizer` — `reviewer/agent/analyzer.py`**

Добавить схему рядом с `_FINDINGS_SCHEMA` (после строки 30):

```python
_SYNTH_SCHEMA = (
    'Верни СТРОГО один JSON-объект без пояснений и markdown:\n'
    '{"findings": [{"file": "<путь>", "category": "correctness|security|performance|style", '
    '"severity": "low|medium|high|critical", "line": <int|null>, "message": "...", '
    '"suggestion": "... или null", "confidence": 0.0}]}\n'
    'Верни ИТОГОВЫЙ список по всему PR: добавь кросс-файловые проблемы '
    '(рассогласование сигнатура↔вызовы), убери дубли. Поле file обязательно у каждой находки.'
)
```

Добавить класс после `LLMVerifier`:

```python
class LLMSynthesizer:
    """Кросс-файловый проход по всем находкам PR: добавляет кросс-файловые проблемы,
    дедуплицирует. Tool-enabled. Fail-open: при неразборе/пустом ответе — возвращает вход."""
    def __init__(self, llm_provider, max_iterations: int = 6):
        self.provider = llm_provider
        self.max_iterations = max_iterations

    def synthesize(self, findings: list[Finding], deps: Deps) -> list[Finding]:
        if not findings:
            return findings
        ctx = ToolContext(
            retriever=deps.retriever, graph=deps.graph,
            overlay_ref=deps.overlay_ref, changed_paths=deps.changed_paths,
            changed_node_ids=[],
            read_file_fn=((lambda p: deps.vcs.get_file_at_ref(p, deps.head_sha))
                          if deps.vcs else None),
            patches=deps.patches, store=getattr(deps.retriever, "store", None))
        tools = make_tools(ctx)
        llm = self.provider.chat_model_with_tools(tools)
        tools_by_name = {t.name: t for t in tools}
        budget = BudgetTracker(self.max_iterations)
        listing = "\n".join(
            f"{i}. [{f.category}/{f.severity}] {f.file}:{f.line} {f.message}"
            for i, f in enumerate(findings))
        pr_ctx = _pr_context(deps, deps.changed_paths)
        human = ((pr_ctx + "\n\n") if pr_ctx else "")
        human += (f"Текущие находки по всему PR:\n{listing}\n\n"
                  "Проверь кросс-файловую согласованность инструментами и верни итоговый список.")
        messages = [SystemMessage(SYNTHESIZE_SYSTEM), HumanMessage(human)]
        try:
            while True:
                budget.tick()
                ai = llm.invoke(messages)
                messages.append(ai)
                if not ai.tool_calls:
                    break
                for call in ai.tool_calls:
                    tool = tools_by_name.get(call["name"])
                    try:
                        result = (tool.invoke(call["args"]) if tool
                                  else f"(неизвестный инструмент: {call['name']})")
                    except Exception as e:
                        result = f"(ошибка инструмента {call['name']}: {e})"
                    messages.append(ToolMessage(str(result), tool_call_id=call["id"]))
        except BudgetExceeded:
            pass
        resp = self.provider.chat_model().invoke(messages + [HumanMessage(_SYNTH_SCHEMA)])
        data = _extract_json(_text_of(resp))
        try:
            parsed = _Findings(**data)
        except Exception:
            return findings   # fail-open
        if not parsed.findings:
            return findings   # пусто -> не теряем вход
        return _to_findings(parsed.findings, default_file=findings[0].file)
```

Добавить `SYNTHESIZE_SYSTEM` в импорт из `prompts` (строка 8):

```python
from reviewer.agent.prompts import ANALYZE_SYSTEM, VERIFY_SYSTEM, SYNTHESIZE_SYSTEM
```

- [ ] **Step 3b: `SYNTHESIZE_SYSTEM` — `reviewer/agent/prompts.py`**

Добавить в конец файла:

```python
SYNTHESIZE_SYSTEM = """Ты — ведущий ревьюер, сводящий замечания по всему PR. Тебе дан \
список находок по отдельным файлам. Твоя задача: (1) найти кросс-файловые проблемы, не \
пойманные пофайлово — рассогласование сигнатуры и её вызовов, переименование и старые \
использования, новый контракт и необновлённые вызовы (проверяй инструментами find_callers, \
read_file, get_changed_file_diff); (2) убрать дубли и слить близкие замечания. Не выдумывай \
проблемы без подтверждения в коде. Верни ИТОГОВЫЙ список находок (с обязательным полем file)."""
```

- [ ] **Step 3c: Узел — `reviewer/agent/nodes.py`**

Добавить в конец файла:

```python
def make_synthesize_node(deps: Deps):
    def synthesize(state: ReviewState):
        return {"verified": deps.synthesizer.synthesize(state["verified"], deps)}
    return synthesize
```

- [ ] **Step 3d: Топология — `reviewer/agent/graph.py`**

Заменить тело `build_graph` на условное добавление узла:

```python
def build_graph(deps: Deps):
    b = StateGraph(ReviewState)
    b.add_node("plan", nodes.plan_node)
    b.add_node("analyze", nodes.make_analyze_node(deps))
    b.add_node("verify", nodes.make_verify_node(deps))
    b.add_node("assemble", nodes.make_assemble_node(deps))
    b.add_node("publish", nodes.make_publish_node(deps))
    b.add_edge(START, "plan")
    b.add_conditional_edges("plan", nodes.fan_out, ["analyze"])
    b.add_edge("analyze", "verify")
    if getattr(deps, "synthesizer", None) is not None:
        b.add_node("synthesize", nodes.make_synthesize_node(deps))
        b.add_edge("verify", "synthesize")
        b.add_edge("synthesize", "assemble")
    else:
        b.add_edge("verify", "assemble")
    b.add_edge("assemble", "publish")
    b.add_edge("publish", END)
    return b.compile()
```

- [ ] **Step 3e: Передать synthesizer из CLI — `reviewer/entrypoints/cli.py`**

Добавить импорт `LLMSynthesizer` (рядом с `LLMAnalyzer, LLMVerifier`, строка 57):

```python
    from reviewer.agent.analyzer import LLMAnalyzer, LLMVerifier, LLMSynthesizer
```

В сборке `Deps` добавить поле (после `changed_status=changed_status`):

```python
                    synthesizer=(LLMSynthesizer(c.llm_provider)
                                 if s.review_synthesis else None),
```

- [ ] **Step 4: Запустить — убедиться, что проходят**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py tests/agent/test_graph.py -q`
Expected: PASS.

- [ ] **Step 5: Прогон всего набора + линт + коммит**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git add reviewer/agent/analyzer.py reviewer/agent/prompts.py reviewer/agent/nodes.py reviewer/agent/graph.py reviewer/entrypoints/cli.py tests/agent/test_analyzer.py tests/agent/test_graph.py
git commit -m "feat(agent): узел кросс-файлового синтеза (за флагом) + топология verify→synthesize→assemble"
```

---

### Task 7: Ручная проверка на rag-demo

**Files:** нет (валидация).

- [ ] **Step 1: Прогнать ревью на открытом PR с включёнными флагами**

```bash
reviewer review mimfort/rag-demo <N>
```

Ожидание: ревью публикуется; в находках видны кросс-файловые замечания и/или меньше шума, чем раньше.

- [ ] **Step 2: Сравнить «было/стало», выключив новые ветки**

```bash
REVIEW_AGENTIC_VERIFY=false REVIEW_SYNTHESIS=false reviewer review mimfort/rag-demo <N>
```

Сравнить набор находок старого и нового пути на одном PR. Зафиксировать наблюдения (что добавилось/убралось).

- [ ] **Step 3: Обновить README (опционально)**

Если поведение/флаги стоит задокументировать — добавить раздел про Тир-3-флаги в `README.md` и закоммитить:

```bash
git add README.md
git commit -m "docs: флаги Тира 3 (agentic verify, synthesis) в README"
```

---

## Замечания по реализации

- **Обратная совместимость:** при `review_agentic_verify=false` и `review_synthesis=false`
  поведение совпадает с текущим (one-shot верификатор, топология без `synthesize`).
- **Стоимость:** агентный верификатор бьёт по находкам выше порога severity или с низким
  confidence — это ограничивает число tool-loop'ов. Синтез — один проход на PR.
- **Voyage TPM:** новые инструменты `read_file`/`get_changed_file_diff` НЕ дёргают эмбеддинги
  (читают git/патчи), `get_definition` ходит в индекс/граф без новых эмбеддингов. Доп. нагрузка
  на Voyage минимальна; основной рост — токены LLM (OpenRouter).
- **fail-open везде:** верификатор и синтез при неразборе JSON не теряют находки.
