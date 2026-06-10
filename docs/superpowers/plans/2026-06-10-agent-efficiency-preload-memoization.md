# Эффективность агента: предзагрузка PR-контекста + мемоизация тулов — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать повторную/лишнюю работу агента (повторные вызовы тулов и перезапрос чужих диффов), чтобы при тех же находках упали число tool-call'ов, обращений к Voyage и `duration_ms`.

**Architecture:** Три независимо коммитимых изменения. (1) Run-level мемоизация результатов тулов + дедуп-заглушка повторов в пределах юнита — в `make_tools`. (2) Прокидка общего на прогон кэша через `Deps.tool_cache` в `ToolContext` всех трёх стадий. (3) Предзагрузка компактного PR-bundle (диффы чужих файлов + изменённые сигнатуры + карты модулей) в промпты analyze/synthesize, чтобы `get_changed_file_diff`/`read_file` по чужим файлам стали не нужны.

**Tech Stack:** Python 3.11+, pytest, langchain_core `StructuredTool`, `functools.wraps` + `inspect.signature` для сохранения схемы аргументов обёрнутых тулов.

**Спек:** `docs/superpowers/specs/2026-06-10-agent-efficiency-preload-memoization-design.md`

---

## File Structure

| Файл | Ответственность | Изменение |
|---|---|---|
| `reviewer/tools/code_tools.py` | определения тулов + `ToolContext` | `ToolContext.cache`; `_memoize`-обёртка; `make_tools` оборачивает 6 тулов |
| `reviewer/agent/state.py` | `Deps` dataclass | поле `tool_cache: dict \| None = None` |
| `reviewer/agent/analyzer.py` | стадии analyze/verify/synthesize | прокидка `cache=deps.tool_cache` в `ToolContext`×3; `_pr_bundle`; врезка bundle в промпты |
| `reviewer/entrypoints/cli.py` | сборка `Deps` на прогон | `tool_cache={}` в `Deps(...)` |
| `tests/tools/test_code_tools.py` | тесты тул-слоя | +4 теста мемоизации |
| `tests/agent/test_analyzer.py` | тесты анализатора | +1 тест прокидки кэша, +3 теста bundle |

**Инвариант корректности кэша:** результат `search_code` зависит от `ctx.changed_node_ids` (различается между файлами PR через graph-expansion в `retrieve`). Поэтому ключ кэша = `(имя_тула, нормализованные_args, сигнатура_changed_node_ids)`. `overlay_ref`/`changed_paths` постоянны в пределах прогона, в ключ не входят (кэш и так живёт один прогон).

---

## Task 1: Мемоизация тулов в `make_tools`

**Files:**
- Modify: `reviewer/tools/code_tools.py`
- Test: `tests/tools/test_code_tools.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/tools/test_code_tools.py`:

```python
class CountingRetriever:
    """Считает вызовы retrieve — для проверки, что кэш экономит обращения к источнику."""
    def __init__(self):
        self.calls = 0
    def retrieve(self, **kw):
        from reviewer.retrieval.retriever import ContextPack
        from reviewer.index.store import Retrieved
        self.calls += 1
        return ContextPack([Retrieved("a.py#f", "a.py", "f", "function", 1, 2, "def f(): ...", 1.0)])


def test_within_unit_duplicate_returns_stub():
    """Повтор того же (tool, args) в пределах юнита -> заглушка, источник дёрнут один раз."""
    ret = CountingRetriever()
    ctx = ToolContext(retriever=ret, graph=FakeGraph(),
                      overlay_ref="pr:1", changed_paths=["a.py"], changed_node_ids=[])
    tools = {t.name: t for t in make_tools(ctx)}
    out1 = tools["search_code"].invoke({"query": "q"})
    out2 = tools["search_code"].invoke({"query": "q"})
    assert "a.py#f" in out1
    assert out2 == "(повтор: результат уже показан выше)"
    assert ret.calls == 1


def test_run_cache_shared_across_units_same_node_ids():
    """Общий run-level кэш: второй юнит (новый make_tools) с тем же changed_node_ids
    берёт результат из кэша, не дёргая источник."""
    ret = CountingRetriever()
    cache: dict = {}
    ctx1 = ToolContext(retriever=ret, graph=FakeGraph(), overlay_ref="pr:1",
                       changed_paths=["a.py"], changed_node_ids=[], cache=cache)
    ctx2 = ToolContext(retriever=ret, graph=FakeGraph(), overlay_ref="pr:1",
                       changed_paths=["a.py"], changed_node_ids=[], cache=cache)
    t1 = {t.name: t for t in make_tools(ctx1)}
    t2 = {t.name: t for t in make_tools(ctx2)}
    t1["search_code"].invoke({"query": "q"})
    out = t2["search_code"].invoke({"query": "q"})
    assert "a.py#f" in out
    assert ret.calls == 1


def test_run_cache_respects_changed_node_ids():
    """Корректность: разные changed_node_ids -> разный ключ -> источник дёргается дважды."""
    ret = CountingRetriever()
    cache: dict = {}
    ctx1 = ToolContext(retriever=ret, graph=FakeGraph(), overlay_ref="pr:1",
                       changed_paths=["a.py"], changed_node_ids=["a.py#f"], cache=cache)
    ctx2 = ToolContext(retriever=ret, graph=FakeGraph(), overlay_ref="pr:1",
                       changed_paths=["a.py"], changed_node_ids=["b.py#g"], cache=cache)
    t1 = {t.name: t for t in make_tools(ctx1)}
    t2 = {t.name: t for t in make_tools(ctx2)}
    t1["search_code"].invoke({"query": "q"})
    t2["search_code"].invoke({"query": "q"})
    assert ret.calls == 2


def test_cache_normalizes_read_file_defaults():
    """read_file(path) и read_file(path, 1, 400) дают один ключ (apply_defaults) -> повтор = заглушка."""
    calls = {"n": 0}
    def rf(p):
        calls["n"] += 1
        return "l1\nl2\nl3"
    ctx = ToolContext(retriever=FakeRetriever(), graph=FakeGraph(), overlay_ref="pr:1",
                      changed_paths=["a.py"], changed_node_ids=[],
                      read_file_fn=rf, cache={})
    tools = {t.name: t for t in make_tools(ctx)}
    tools["read_file"].invoke({"path": "a.py"})
    tools["read_file"].invoke({"path": "a.py", "start": 1, "end": 400})
    assert calls["n"] == 1
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `.venv/bin/pytest tests/tools/test_code_tools.py -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'cache'` (у `ToolContext` ещё нет поля `cache`), и `test_within_unit_duplicate_returns_stub` падает на assert заглушки.

- [ ] **Step 3: Добавить поле `cache` в `ToolContext` и импорты**

В начале `reviewer/tools/code_tools.py` заменить шапку импортов:

```python
from __future__ import annotations
import functools
import inspect
import json
from dataclasses import dataclass, field
from langchain_core.tools import StructuredTool

_DUP_STUB = "(повтор: результат уже показан выше)"
```

В `ToolContext` добавить поле (после `store`):

```python
    store: object = None                   # индекс-стор для get_definition
    cache: dict | None = None              # run-level кэш результатов тулов (общий на прогон)
```

- [ ] **Step 4: Добавить `_memoize` и обернуть тулы в `make_tools`**

Добавить функцию `_memoize` перед `make_tools` (или сразу после импортов):

```python
def _memoize(fn, ctx_sig, seen, cache):
    """Оборачивает tool-функцию: run-level кэш результатов (cache) + дедуп-заглушка
    повторов в пределах юнита (seen). Ключ = (имя, нормализованные args, ctx_sig).
    functools.wraps сохраняет имя/докстринг/сигнатуру -> StructuredTool строит ту же схему."""
    sig = inspect.signature(fn)

    def _key(args, kwargs):
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return (fn.__name__,
                json.dumps(bound.arguments, sort_keys=True, ensure_ascii=False, default=str),
                ctx_sig)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key = _key(args, kwargs)
        if key in seen:
            return _DUP_STUB
        if cache is not None and key in cache:
            result = cache[key]
        else:
            result = fn(*args, **kwargs)
            if cache is not None:
                cache[key] = result
        seen.add(key)
        return result

    return wrapper
```

Заменить финальный `return [...]` в `make_tools` на:

```python
    seen: set = set()
    ctx_sig = tuple(sorted(ctx.changed_node_ids or []))
    raw = [search_code, get_related_symbols, read_file,
           get_definition, find_callers, get_changed_file_diff]
    return [StructuredTool.from_function(_memoize(fn, ctx_sig, seen, ctx.cache)) for fn in raw]
```

- [ ] **Step 5: Запустить — убедиться, что проходят (и старые тоже)**

Run: `.venv/bin/pytest tests/tools/test_code_tools.py -q`
Expected: PASS — все тесты (новые 4 + прежние 11).

- [ ] **Step 6: Линт**

Run: `.venv/bin/ruff check reviewer/tools/code_tools.py`
Expected: без ошибок.

- [ ] **Step 7: Коммит**

```bash
git add reviewer/tools/code_tools.py tests/tools/test_code_tools.py
git commit -m "feat(tools): run-level мемоизация тулов + дедуп повторных вызовов в юните"
```

---

## Task 2: Прокидка `Deps.tool_cache` через анализатор и CLI

**Files:**
- Modify: `reviewer/agent/state.py`
- Modify: `reviewer/agent/analyzer.py` (3 места конструирования `ToolContext`)
- Modify: `reviewer/entrypoints/cli.py:294` (блок `Deps(...)`)
- Test: `tests/agent/test_analyzer.py`

- [ ] **Step 1: Написать падающий тест прокидки кэша**

Добавить в конец `tests/agent/test_analyzer.py`:

```python
class CountingRetrieverA:
    """Считает retrieve — проверка, что deps.tool_cache доходит до ToolContext."""
    def __init__(self):
        self.calls = 0
    def retrieve(self, **kw):
        from reviewer.retrieval.retriever import ContextPack
        from reviewer.index.store import Retrieved
        self.calls += 1
        return ContextPack([Retrieved("a.py#f", "a.py", "f", "function", 1, 2, "x", 1.0)])


def test_analyze_shares_tool_cache_across_units():
    """Два analyze с общим deps.tool_cache не пересчитывают одинаковый search_code
    (одинаковые changed_node_ids=[] у обоих юнитов)."""
    tool_call = AIMessage(content="", tool_calls=[
        {"name": "search_code", "args": {"query": "q"}, "id": "t1", "type": "tool_call"}])
    final_json = AIMessage(content='{"findings":[]}')
    ret = CountingRetrieverA()
    deps = _deps(retriever=ret, tool_cache={})
    LLMAnalyzer(FakeProvider([tool_call, final_json], "FB"), max_iterations=10).analyze(
        ReviewUnit("a.py", [], "code"), deps)
    LLMAnalyzer(FakeProvider([tool_call, final_json], "FB"), max_iterations=10).analyze(
        ReviewUnit("b.py", [], "code"), deps)
    assert ret.calls == 1
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py::test_analyze_shares_tool_cache_across_units -q`
Expected: FAIL — `TypeError: Deps.__init__() got an unexpected keyword argument 'tool_cache'`.

- [ ] **Step 3: Добавить поле в `Deps`**

В `reviewer/agent/state.py`, в dataclass `Deps`, после `skipped_paths`:

```python
    skipped_paths: list[str] | None = None  # файлы сверх review_max_files (попадут в сводку)
    tool_cache: dict | None = None          # run-level кэш результатов тулов (мемоизация)
```

- [ ] **Step 4: Прокинуть кэш в три `ToolContext` анализатора**

В `reviewer/agent/analyzer.py` в КАЖДОМ из трёх мест построения `ToolContext` (в `LLMAnalyzer.analyze`, `LLMVerifier._verify_one`, `LLMSynthesizer.synthesize`) добавить аргумент `cache`. Каждый из них заканчивается строкой:

```python
            patches=deps.patches, store=getattr(deps.retriever, "store", None))
```

Заменить (во всех трёх) на:

```python
            patches=deps.patches, store=getattr(deps.retriever, "store", None),
            cache=getattr(deps, "tool_cache", None))
```

- [ ] **Step 5: Инициализировать кэш в CLI**

В `reviewer/entrypoints/cli.py` в блоке `deps = Deps(` (около строки 294) добавить поле (например, рядом с `patches=...`):

```python
            patches={f.path: f.patch for f in files},
            tool_cache={},
```

- [ ] **Step 6: Запустить тест и весь модуль анализатора**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -q`
Expected: PASS — новый тест и все прежние тесты модуля.

- [ ] **Step 7: Проверить, что CLI-врезка на месте**

Run: `grep -n "tool_cache={}" reviewer/entrypoints/cli.py`
Expected: одна строка с `tool_cache={},` внутри блока `Deps(`.

- [ ] **Step 8: Линт**

Run: `.venv/bin/ruff check reviewer/agent/state.py reviewer/agent/analyzer.py reviewer/entrypoints/cli.py`
Expected: без ошибок.

- [ ] **Step 9: Коммит**

```bash
git add reviewer/agent/state.py reviewer/agent/analyzer.py reviewer/entrypoints/cli.py tests/agent/test_analyzer.py
git commit -m "feat(agent): общий на прогон кэш тулов (Deps.tool_cache) во всех стадиях"
```

---

## Task 3: PR-bundle — предзагрузка чужих диффов и структуры в промпты

**Files:**
- Modify: `reviewer/agent/analyzer.py` (новый `_pr_bundle`; врезка в `analyze` и `synthesize`)
- Test: `tests/agent/test_analyzer.py`

- [ ] **Step 1: Написать падающие тесты**

В `tests/agent/test_analyzer.py` расширить импорт из `reviewer.agent.analyzer`, добавив `_pr_bundle`:

```python
from reviewer.agent.analyzer import (
    LLMAnalyzer, LLMVerifier, LLMSynthesizer,
    _pr_context, _to_findings, _FindingModel, _window,
    _file_context, _signature_changes, _pr_bundle,
)
```

Добавить тесты в конец файла:

```python
def _combine_human(messages):
    """Склеить текст всех HumanMessage (учитывая cacheable-блоки списком)."""
    out = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            c = msg.content
            out.append("".join(p.get("text", "") for p in c if isinstance(p, dict))
                       if isinstance(c, list) else str(c))
    return "\n".join(out)


def test_pr_bundle_excludes_current_and_includes_signatures():
    patches = {"a.py": "@@ -1 +1 @@\n-def f(x):\n+def f(x, y):",
               "b.py": "@@ -1 +1 @@\n+z = 1"}
    sources = {"a.py": "def f(x, y):\n    return x"}
    deps = _deps(changed_paths=["a.py", "b.py"], patches=patches, sources=sources)
    out = _pr_bundle(deps, ["a.py", "b.py"], current_path="a.py")
    assert "--- b.py ---" in out
    assert "--- a.py ---" not in out                 # текущий файл исключён
    assert "Изменённые сигнатуры в PR" in out
    assert "+ def f(x, y):" in out
    assert "Структура изменённых модулей" in out     # из sources a.py


def test_pr_bundle_caps_total_diff_lines():
    big_patch = "\n".join(f"+line{i}" for i in range(1, 1001))   # 1000 строк
    patches = {f"f{k}.py": big_patch for k in range(5)}          # 5 файлов × 1000
    deps = _deps(changed_paths=list(patches), patches=patches)
    out = _pr_bundle(deps, list(patches))
    assert "опущены" in out                          # часть файлов не влезла в кап


def test_analyze_prompt_includes_other_file_diffs_bundle():
    captured: list = []

    class CaptureLLM:
        def invoke(self, messages):
            captured.extend(messages)
            return AIMessage(content='{"findings":[]}')
        def bind_tools(self, tools):
            return self

    class CapProvider:
        def chat_model_with_tools(self, tools, model=None):
            return CaptureLLM()
        def chat_model(self, model=None):
            return FinalLLM("FB")

    deps = _deps(changed_paths=["a.py", "b.py"],
                 patches={"a.py": "@@ -1 +1 @@\n+a", "b.py": "@@ -1 +1 @@\n-old\n+newbie"})
    LLMAnalyzer(CapProvider(), max_iterations=2).analyze(ReviewUnit("a.py", [], "code"), deps)
    human = _combine_human(captured)
    assert "Диффы других изменённых файлов PR" in human
    assert "--- b.py ---" in human and "+newbie" in human
    assert "--- a.py ---" not in human               # текущий файл не дублируется в bundle
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -k "pr_bundle or other_file_diffs" -q`
Expected: FAIL — `ImportError: cannot import name '_pr_bundle'`.

- [ ] **Step 3: Реализовать `_pr_bundle`**

В `reviewer/agent/analyzer.py` рядом с `_signature_changes` добавить константу и функцию:

```python
_BUNDLE_LINE_CAP = 1500     # суммарный кап строк диффов в PR-bundle


def _pr_bundle(deps, changed_paths: list[str], current_path: str | None = None) -> str:
    """Компактный обзор PR для предзагрузки в промпт: диффы изменённых файлов
    (кроме current_path), изменённые сигнатуры и карты сигнатур модулей.

    Диффы режутся по суммарному капу строк; остаток помечается. Цель — чтобы агент
    не дёргал get_changed_file_diff/read_file по чужим файлам (тулы остаются как fallback)."""
    patches = getattr(deps, "patches", None) or {}
    parts: list[str] = []

    diff_blocks: list[str] = []
    used = 0
    omitted = 0
    for path in changed_paths:
        if path == current_path:
            continue
        patch = patches.get(path)
        if not patch:
            continue
        plines = patch.splitlines()
        if used + len(plines) > _BUNDLE_LINE_CAP:
            omitted += 1
            continue
        used += len(plines)
        diff_blocks.append(f"--- {path} ---\n{patch}")
    if diff_blocks:
        head = ("Диффы других изменённых файлов PR:" if current_path
                else "Диффы изменённых файлов PR:")
        block = head + "\n" + "\n\n".join(diff_blocks)
        if omitted:
            block += (f"\n(ещё {omitted} файлов опущены — "
                      "используй get_changed_file_diff при необходимости)")
        parts.append(block)

    sig_changes = _signature_changes(patches)
    if sig_changes:
        parts.append("Изменённые сигнатуры в PR:\n" + sig_changes)

    sources = getattr(deps, "sources", None) or {}
    sig_maps: list[str] = []
    for path in changed_paths:
        src = sources.get(path)
        if not src:
            continue
        sigs = _module_signatures(path, src)
        if sigs:
            sig_maps.append(f"{path}:\n{sigs}")
    if sig_maps:
        parts.append("Структура изменённых модулей:\n" + "\n\n".join(sig_maps))

    return "\n\n".join(parts)
```

- [ ] **Step 4: Врезать bundle в `analyze`**

В `LLMAnalyzer.analyze`, сразу после строки

```python
        human = (pr_ctx + "\n\n") if pr_ctx else ""
```

добавить:

```python
        bundle = _pr_bundle(deps, deps.changed_paths, current_path=unit.path)
        if bundle:
            human += bundle + "\n\n"
```

- [ ] **Step 5: Врезать bundle в `synthesize` (заменив ручной блок сигнатур)**

В `LLMSynthesizer.synthesize` заменить блок:

```python
        pr_ctx = _pr_context(deps, deps.changed_paths)
        human = ((pr_ctx + "\n\n") if pr_ctx else "")
        sig_changes = _signature_changes(getattr(deps, "patches", None) or {})
        if sig_changes:
            human += ("Изменённые сигнатуры в PR (проверь согласованность с вызовами "
                      f"через find_callers):\n{sig_changes}\n\n")
        human += (f"Текущие находки по всему PR:\n{listing}\n\n"
                  "Проверь кросс-файловую согласованность инструментами и верни итоговый список.")
```

на:

```python
        pr_ctx = _pr_context(deps, deps.changed_paths)
        human = ((pr_ctx + "\n\n") if pr_ctx else "")
        bundle = _pr_bundle(deps, deps.changed_paths)
        if bundle:
            human += bundle + "\n\n"
        if _signature_changes(getattr(deps, "patches", None) or {}):
            human += ("Проверь согласованность изменённых сигнатур с их вызовами "
                      "через find_callers.\n\n")
        human += (f"Текущие находки по всему PR:\n{listing}\n\n"
                  "Проверь кросс-файловую согласованность инструментами и верни итоговый список.")
```

- [ ] **Step 6: Запустить новые тесты**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -k "pr_bundle or other_file_diffs" -q`
Expected: PASS — 3 новых теста.

- [ ] **Step 7: Запустить весь модуль анализатора (регрессия)**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -q`
Expected: PASS — в т.ч. `test_synthesize_prompt_includes_signature_changes` (bundle содержит «Изменённые сигнатуры в PR» и «+ def connect(host, port, timeout):»).

- [ ] **Step 8: Линт**

Run: `.venv/bin/ruff check reviewer/agent/analyzer.py`
Expected: без ошибок.

- [ ] **Step 9: Коммит**

```bash
git add reviewer/agent/analyzer.py tests/agent/test_analyzer.py
git commit -m "feat(agent): предзагрузка PR-bundle (диффы чужих файлов + сигнатуры) в analyze/synthesize"
```

---

## Финальная проверка

- [ ] **Прогнать весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS, integration-тесты исключены по умолчанию (`-m 'not integration'`).

- [ ] **Линт всего проекта**

Run: `.venv/bin/ruff check .`
Expected: без ошибок.

- [ ] **(Опционально) Замер эффекта на реальном PR**

После поднятого окружения (`docker compose up -d`) и валидного `.env`:

Run: `reviewer review mimfort/rag-demo 14 --dry-run`
Then: сравнить новую запись в `review_runs`/`review_steps` с прогоном 12 — меньше `kind='tool_call'`, меньше `duration_ms`, тот же набор находок. Критерий успеха из спека.

---

## Self-Review (выполнено при написании плана)

**Spec coverage:**
- (A) предзагрузка PR-bundle → Task 3. ✓
- (B) run-level кэш результатов тулов → Task 1 (механизм) + Task 2 (прокидка/инициализация). ✓
- (C) дедуп-заглушка в пределах юнита → Task 1 (`seen` + `_DUP_STUB`). ✓
- Дедуп эмбеддингов запросов → следствие кэша `search_code`, проверяется `CountingRetriever` (Task 1) и `CountingRetrieverA` (Task 2). ✓
- Edge: кап большого PR → Task 3 `test_pr_bundle_caps_total_diff_lines`. ✓
- Edge: корректность кэша по `changed_node_ids` → Task 1 `test_run_cache_respects_changed_node_ids`. ✓
- Fail-soft: `cache=None` отключает кэш, не ломая тулы; bundle при пустых patches = "" (старое поведение). ✓
- Опциональные счётчики кэша в `TraceLog` — намеренно вне плана (вынесено в спеке как опция).

**Placeholder scan:** плейсхолдеров нет — весь код приведён дословно.

**Type consistency:** `ToolContext.cache: dict | None`, `Deps.tool_cache: dict | None`, `_memoize(fn, ctx_sig, seen, cache)`, `_pr_bundle(deps, changed_paths, current_path=None)`, заглушка `_DUP_STUB` — имена согласованы между задачами и тестами.
