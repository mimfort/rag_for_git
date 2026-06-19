# PRI-138 — Оптимизация выдачи `search_codebase` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сократить избыточность session-less выдачи `search_codebase` (дедуп вложенных чанков, построчные номера, подрезка тестов) — общий движковый фикс для скиллов `ask` и `solve-task`.

**Architecture:** Все изменения на session-less пути: чистые хелперы `_dedupe_overlapping`/`_is_test_path` и opt-in флаг номеров строк в `ContextPack.as_context` (`reviewer/retrieval/retriever.py`); подключение в `Retriever.search_base`; проброс `include_tests` через `MCPReviewService.search_codebase` и MCP-тул. PR-ревью путь (`Retriever.retrieve` / `reviewer/tools/code_tools.py`) не трогаем — это гарантируется дефолтом `line_numbers=False` и тем, что дедуп/фильтр живут только в `search_base`.

**Tech Stack:** Python 3.11–3.13, pytest, tree-sitter (chunker), FastMCP (MCP-сервер), ParadeDB/Neo4j/Voyage (только для замера крит.4).

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения. Сохранять стиль в новом коде.
- Ruff: line-length **100**, target **py311** (`.venv/bin/ruff check .`).
- Коммиты: **Conventional Commits на русском** (`feat(retrieval): …`, `test(mcp): …`), **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- Тесты: `.venv/bin/pytest -q` по умолчанию исключает `integration`. Unit-тесты на фейках, внешние API не дёргают.
- Ветка работы: `feat/pri-138-search-codebase-output` (уже создана, спек закоммичен).
- **Вне объёма:** режим `headers-only`; изменения PR-пути (`retrieve`/`code_tools.py`); дедуп/фильтр в `store.hybrid_search`; новый eval-харнесс.

---

### Task 1: Хелпер `_dedupe_overlapping` (дедуп вложенных чанков)

**Files:**
- Modify: `reviewer/retrieval/retriever.py` (добавить module-level функцию после импортов, до класса `Retriever`)
- Test: `tests/retrieval/test_output_shaping.py` (создать)

**Interfaces:**
- Produces: `_dedupe_overlapping(items: list) -> list` — принимает объекты с атрибутами `path: str`, `start_line: int`, `end_line: int`; возвращает подсписок (те же объекты), где удалён каждый чанк, чей диапазон строк полностью вложен в диапазон другого удержанного чанка того же `path`. Порядок выживших стабилен относительно входа.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/retrieval/test_output_shaping.py`:

```python
from types import SimpleNamespace

from reviewer.retrieval.retriever import _dedupe_overlapping


def _it(path, start, end, node_id=None):
    return SimpleNamespace(path=path, start_line=start, end_line=end,
                           node_id=node_id or f"{path}#s{start}")


def test_dedupe_drops_method_nested_in_class():
    cls = _it("a.py", 1, 50, "a.py#Foo")
    method = _it("a.py", 10, 20, "a.py#Foo.bar")
    assert [x.node_id for x in _dedupe_overlapping([cls, method])] == ["a.py#Foo"]


def test_dedupe_drops_class_when_method_comes_first():
    method = _it("a.py", 10, 20, "a.py#Foo.bar")
    cls = _it("a.py", 1, 50, "a.py#Foo")
    # метод пришёл раньше, но самый широкий (класс) должен остаться
    assert [x.node_id for x in _dedupe_overlapping([method, cls])] == ["a.py#Foo"]


def test_dedupe_keeps_partial_overlap():
    a = _it("a.py", 10, 30)
    b = _it("a.py", 20, 40)
    assert _dedupe_overlapping([a, b]) == [a, b]


def test_dedupe_independent_per_path():
    a = _it("a.py", 1, 50)
    b = _it("b.py", 10, 20)
    assert _dedupe_overlapping([a, b]) == [a, b]


def test_dedupe_preserves_order_of_survivors():
    a = _it("a.py", 1, 5)
    b = _it("b.py", 1, 5)
    c = _it("c.py", 1, 5)
    assert _dedupe_overlapping([a, b, c]) == [a, b, c]
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/retrieval/test_output_shaping.py -q`
Expected: FAIL — `ImportError: cannot import name '_dedupe_overlapping'`.

- [ ] **Step 3: Реализовать хелпер**

В `reviewer/retrieval/retriever.py` после `log = logging.getLogger(__name__)` и до `@dataclass class ContextPack`:

```python
def _dedupe_overlapping(items: list) -> list:
    """Убрать вложенные дубли чанков.

    Чанк отбрасывается, если его диапазон ``[start_line, end_line]`` полностью
    вложен в диапазон другого удержанного чанка того же ``path`` (правило
    «оставить самый широкий»: класс уже включает текст своих методов). Чанки с
    одинаковым диапазоном и частично пересекающиеся сохраняются. Порядок
    выживших стабилен относительно входа.
    """
    def _nested_in(inner, outer) -> bool:
        return (
            outer.path == inner.path
            and outer.start_line <= inner.start_line
            and inner.end_line <= outer.end_line
            and (outer.start_line, outer.end_line) != (inner.start_line, inner.end_line)
        )

    kept: list = []
    for it in items:
        if any(_nested_in(it, k) for k in kept):
            continue
        kept = [k for k in kept if not _nested_in(k, it)]
        kept.append(it)
    return kept
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/retrieval/test_output_shaping.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/retrieval/retriever.py tests/retrieval/test_output_shaping.py
git commit -m "feat(retrieval): хелпер _dedupe_overlapping — дроп вложенных чанков"
```

---

### Task 2: Хелпер `_is_test_path` (детект тест-файлов)

**Files:**
- Modify: `reviewer/retrieval/retriever.py` (module-level функция рядом с `_dedupe_overlapping`)
- Test: `tests/retrieval/test_output_shaping.py` (дополнить)

**Interfaces:**
- Produces: `_is_test_path(path: str) -> bool` — `True`, если путь содержит сегмент `tests` **или** basename соответствует `test_*.py` / `*_test.py`.

- [ ] **Step 1: Написать падающий тест**

Дописать в `tests/retrieval/test_output_shaping.py`:

```python
import pytest

from reviewer.retrieval.retriever import _is_test_path


@pytest.mark.parametrize("path", [
    "tests/retrieval/test_x.py",
    "reviewer/index/test_store.py",
    "pkg/foo_test.py",
    "tests/conftest.py",
])
def test_is_test_path_true(path):
    assert _is_test_path(path) is True


@pytest.mark.parametrize("path", [
    "reviewer/retrieval/retriever.py",
    "reviewer/index/store.py",
    "contests/runner.py",   # 'contests' не равно сегменту 'tests'
])
def test_is_test_path_false(path):
    assert _is_test_path(path) is False
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/retrieval/test_output_shaping.py -q -k is_test_path`
Expected: FAIL — `ImportError: cannot import name '_is_test_path'`.

- [ ] **Step 3: Реализовать хелпер**

В `reviewer/retrieval/retriever.py` рядом с `_dedupe_overlapping`:

```python
def _is_test_path(path: str) -> bool:
    """Путь относится к тестам: содержит сегмент ``tests`` или basename
    соответствует ``test_*.py`` / ``*_test.py``.
    """
    p = path.replace("\\", "/")
    parts = p.split("/")
    if "tests" in parts:
        return True
    base = parts[-1]
    return base.startswith("test_") or base.endswith("_test.py")
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/retrieval/test_output_shaping.py -q -k is_test_path`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/retrieval/retriever.py tests/retrieval/test_output_shaping.py
git commit -m "feat(retrieval): хелпер _is_test_path — детект тест-файлов"
```

---

### Task 3: Построчные номера в `ContextPack.as_context` (opt-in)

**Files:**
- Modify: `reviewer/retrieval/retriever.py` (метод `ContextPack.as_context`, текущие строки 16-28)
- Test: `tests/retrieval/test_output_shaping.py` (дополнить)

**Interfaces:**
- Consumes: ничего из предыдущих задач.
- Produces: `ContextPack.as_context(self, line_numbers: bool = False) -> str`. При `False` — байт-в-байт прежний вывод. При `True` — каждая строка тела префиксуется абсолютным номером (`{n:>5} | {code}`, где `n = start_line + i`); заголовок `// {node_id} ({path}:{start}-{end})` сохраняется.

- [ ] **Step 1: Написать падающий тест**

Дописать в `tests/retrieval/test_output_shaping.py`:

```python
from reviewer.retrieval.retriever import ContextPack


def _node():
    return SimpleNamespace(node_id="a.py#f", path="a.py", start_line=10,
                           end_line=11, text="def f():\n    pass")


def test_as_context_default_unchanged():
    out = ContextPack(items=[_node()]).as_context()
    assert out == "// a.py#f (a.py:10-11)\ndef f():\n    pass"


def test_as_context_with_line_numbers():
    out = ContextPack(items=[_node()]).as_context(line_numbers=True)
    assert "// a.py#f (a.py:10-11)" in out
    assert "   10 | def f():" in out
    assert "   11 |     pass" in out


def test_as_context_line_numbers_respect_truncation():
    big = SimpleNamespace(node_id="a.py#f", path="a.py", start_line=1,
                          end_line=3, text="x\ny\nz")
    out = ContextPack(items=[big], max_chars=20).as_context(line_numbers=True)
    assert out.endswith("[...truncated]")
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/retrieval/test_output_shaping.py -q -k as_context`
Expected: FAIL — `test_as_context_with_line_numbers` падает (`as_context()` не принимает `line_numbers`) → `TypeError`.

- [ ] **Step 3: Реализовать**

Заменить метод `as_context` в `reviewer/retrieval/retriever.py` (строки 16-28) на:

```python
    def as_context(self, line_numbers: bool = False) -> str:
        parts = []
        for it in self.items:
            header = f"// {it.node_id} ({it.path}:{it.start_line}-{it.end_line})"
            if line_numbers:
                body = "\n".join(
                    f"{it.start_line + i:>5} | {line}"
                    for i, line in enumerate(it.text.split("\n"))
                )
            else:
                body = it.text
            parts.append(f"{header}\n{body}")
        text = "\n\n".join(parts)
        limit = 0
        if self.max_chars > 0:
            limit = self.max_chars
        elif self.max_tokens > 0:
            limit = self.max_tokens * 4
        if limit > 0 and len(text) > limit:
            text = text[:limit] + "\n[...truncated]"
        return text
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/retrieval/test_output_shaping.py -q`
Expected: PASS (все).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/retrieval/retriever.py tests/retrieval/test_output_shaping.py
git commit -m "feat(retrieval): opt-in номера строк в ContextPack.as_context"
```

---

### Task 4: Подключить дедуп + фильтр тестов в `Retriever.search_base`

**Files:**
- Modify: `reviewer/retrieval/retriever.py` (метод `Retriever.search_base`, текущие строки 60-98)
- Test: `tests/retrieval/test_search_base.py` (дополнить + расширить фейк `_Hit`)

**Interfaces:**
- Consumes: `_dedupe_overlapping` (Task 1), `_is_test_path` (Task 2).
- Produces: `Retriever.search_base(self, repo, query, top_k=10, candidates=50, *, branch="", include_tests=False) -> ContextPack`. По умолчанию выдача без тест-чанков и без вложенных дублей; `include_tests=True` возвращает тест-чанки. Фильтр и дедуп применяются к `merged.values()` **до** rerank/`top_k`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/retrieval/test_search_base.py` расширить фейк `_Hit` (добавить опциональные диапазоны, обратносовместимо) — заменить его `__init__` на:

```python
class _Hit:
    def __init__(self, node_id, score=1.0, start_line=1, end_line=2):
        self.node_id = node_id
        self.path, self.symbol_fqn = node_id.split("#", 1)
        self.kind = "function"
        self.start_line = start_line
        self.end_line = end_line
        self.text = "body"
        self.score = score
```

Дописать тесты в конец файла:

```python
def test_search_base_dedupes_nested_chunks():
    hits = [_Hit("a.py#Foo", start_line=1, end_line=50),
            _Hit("a.py#Foo.bar", start_line=10, end_line=20)]
    r = Retriever(_FakeStore(hits), graph=None, embedder=_FakeEmbedder(), reranker=None)
    pack = r.search_base("a/x", "q", top_k=5)
    assert [it.node_id for it in pack.items] == ["a.py#Foo"]


def test_search_base_filters_tests_by_default():
    hits = [_Hit("a.py#f"), _Hit("tests/test_a.py#t")]
    r = Retriever(_FakeStore(hits), graph=None, embedder=_FakeEmbedder(), reranker=None)
    pack = r.search_base("a/x", "q", top_k=5)
    assert [it.node_id for it in pack.items] == ["a.py#f"]


def test_search_base_include_tests_keeps_tests():
    hits = [_Hit("a.py#f"), _Hit("tests/test_a.py#t")]
    r = Retriever(_FakeStore(hits), graph=None, embedder=_FakeEmbedder(), reranker=None)
    pack = r.search_base("a/x", "q", top_k=5, include_tests=True)
    assert {it.node_id for it in pack.items} == {"a.py#f", "tests/test_a.py#t"}
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/retrieval/test_search_base.py -q -k "dedupes or filters or include_tests"`
Expected: FAIL — `search_base()` не принимает `include_tests` (`TypeError`) и/или дедуп/фильтр не применяются.

- [ ] **Step 3: Реализовать**

В `reviewer/retrieval/retriever.py` изменить сигнатуру `search_base` (строка 60):

```python
    def search_base(self, repo, query, top_k=10, candidates=50, *, branch="",
                    include_tests=False) -> ContextPack:
```

И в теле метода заменить строку `items = list(merged.values())` (строка 90) на:

```python
        items = list(merged.values())
        if not include_tests:
            items = [it for it in items if not _is_test_path(it.path)]
        items = _dedupe_overlapping(items)
```

(Остальное тело — проверка реранкера и возврат — без изменений.)

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/retrieval/test_search_base.py tests/retrieval/test_retriever_branch.py -q`
Expected: PASS (включая прежние тесты `test_search_base_*` — они используют не-тестовые пути `a.py#f1` и т.п., фильтр их не трогает).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/retrieval/retriever.py tests/retrieval/test_search_base.py
git commit -m "feat(retrieval): search_base — дедуп + подрезка тестов (include_tests)"
```

---

### Task 5: `MCPReviewService.search_codebase` — номера строк + проброс `include_tests`

**Files:**
- Modify: `reviewer/mcp/service.py` (метод `search_codebase`, текущие строки 347-364)
- Test: `tests/mcp/test_service.py` (обновить существующие тесты)

**Interfaces:**
- Consumes: `Retriever.search_base(..., include_tests=...)` (Task 4), `ContextPack.as_context(line_numbers=...)` (Task 3).
- Produces: `MCPReviewService.search_codebase(self, repo, query, top_k=10, branch=None, include_tests=False) -> str` — пробрасывает `include_tests` в `search_base` и рендерит `pack.as_context(line_numbers=True)`.

- [ ] **Step 1: Обновить тесты (станут падающими)**

В `tests/mcp/test_service.py` заменить `test_search_codebase_delegates_to_retriever` на:

```python
def test_search_codebase_delegates_to_retriever() -> None:
    """search_codebase зовёт retriever.search_base (include_tests=False)
    и рендерит ContextPack с номерами строк."""
    svc = _make_mcp_service()
    svc.components.retriever.search_base.return_value.as_context.return_value = "auth.py#logout\nbody"
    out = svc.search_codebase("a/b", "logout", top_k=5)
    assert "auth.py#logout" in out
    svc.components.retriever.search_base.assert_called_once_with(
        "a/b", "logout", top_k=5, branch=svc.settings.primary_branch(),
        include_tests=False)
    svc.components.retriever.search_base.return_value.as_context.assert_called_once_with(
        line_numbers=True)
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_service.py::test_search_codebase_delegates_to_retriever -q`
Expected: FAIL — `search_base` зовётся без `include_tests`, `as_context` без `line_numbers`.

- [ ] **Step 3: Реализовать**

В `reviewer/mcp/service.py` заменить тело `search_codebase` (строки 347-364):

```python
    def search_codebase(self, repo: str, query: str, top_k: int = 10,
                        branch: str | None = None,
                        include_tests: bool = False) -> str:
        """Гибрид-поиск по base-индексу репозитория (без PR-сессии) — для /solve-task.

        branch — отслеживаемая ветка (allowlist REVIEW_BRANCHES); по умолчанию
        первичная. Поиск идёт по индексу указанной ветки (base:<branch>).
        Выдача: без вложенных дублей и (по умолчанию) без тест-чанков, с
        построчными номерами для цитирования path:line без повторного Read.
        include_tests=True возвращает тест-чанки.
        """
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        try:
            pack = self.components.retriever.search_base(
                repo, query, top_k=top_k, branch=resolved, include_tests=include_tests)
        except Exception:
            log.warning("search_codebase: сбой поиска", exc_info=True)
            return "(ничего не найдено)"
        return pack.as_context(line_numbers=True) or "(ничего не найдено)"
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_service.py -q -k search_codebase`
Expected: PASS — прочие `test_search_codebase_*` (empty/error/repo/branch) не пинят новые kwargs и остаются зелёными.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "feat(mcp): search_codebase — номера строк + проброс include_tests"
```

---

### Task 6: `MCPReviewService.definition` — единый рендер через `as_context`

**Files:**
- Modify: `reviewer/mcp/service.py` (импорт `ContextPack`; метод `definition`, текущие строки 400-425)
- Test: `tests/mcp/test_service.py` (обновить два теста `definition`)

**Interfaces:**
- Consumes: `ContextPack.as_context(line_numbers=True)` (Task 3), `Retriever.search_base(..., include_tests=True)` (Task 4).
- Produces: `definition` рендерит обе ветки (graph-hit и semantic-фолбэк) через `ContextPack(...).as_context(line_numbers=True)` (убирает дублирование формата заголовка на бывшей строке 418); фолбэк зовёт `search_base(..., include_tests=True)` — символы-тесты находятся.

- [ ] **Step 1: Обновить тесты (станут падающими)**

В `tests/mcp/test_service.py` заменить `test_definition_uses_graph_then_store` и `test_definition_falls_back_to_search_base` на:

```python
def test_definition_uses_graph_then_store() -> None:
    """definition: find_symbol → fetch_nodes → рендер через as_context (с номерами строк)."""
    svc = _make_mcp_service()
    svc.components.graph.find_symbol.return_value = ["a.py#foo"]
    node = SimpleNamespace(node_id="a.py#foo", path="a.py",
                           start_line=10, end_line=12, text="def foo(): ...")
    svc.components.store.fetch_nodes.return_value = [node]
    out = svc.definition("a/b", "foo")
    assert "a.py#foo" in out and "def foo()" in out and "a.py:10-12" in out
    assert "   10 | def foo()" in out  # номера строк присутствуют
    svc.components.graph.find_symbol.assert_called_once_with(
        "a/b", "foo", branch=svc.settings.primary_branch())
    svc.components.store.fetch_nodes.assert_called_once_with(
        "a/b", ["a.py#foo"], None, [], base_ref="base:main")


def test_definition_falls_back_to_search_base() -> None:
    """Граф пуст → фолбэк на retriever.search_base (include_tests=True)."""
    svc = _make_mcp_service()
    svc.components.graph.find_symbol.return_value = []
    svc.components.retriever.search_base.return_value.as_context.return_value = "semantic hit"
    out = svc.definition("a/b", "foo")
    assert "semantic hit" in out
    svc.components.retriever.search_base.assert_called_once_with(
        "a/b", "foo", top_k=3, branch=svc.settings.primary_branch(),
        include_tests=True)
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_service.py -q -k definition`
Expected: FAIL — нет номеров строк в graph-ветке; `search_base` зовётся без `include_tests=True`.

- [ ] **Step 3: Реализовать**

В `reviewer/mcp/service.py` добавить импорт (рядом с прочими `from reviewer...`, напр. после строки 15 `from reviewer.index.refs import base_ref`):

```python
from reviewer.retrieval.retriever import ContextPack
```

Заменить тело `definition` (строки 400-425):

```python
    def definition(self, repo: str, symbol: str,
                   branch: str | None = None) -> str:
        """Где определён символ + исходник (граф → индекс → семантический фолбэк),
        без PR-сессии. Тесты не отфильтровываются — символ может быть тестом."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        try:
            ids: list[str] = []
            if self.components.graph is not None:
                ids = self.components.graph.find_symbol(
                    repo, symbol, branch=resolved)
            if ids:
                nodes = self.components.store.fetch_nodes(
                    repo, ids[:3], None, [], base_ref=base_ref(resolved))
                if nodes:
                    return ContextPack(items=nodes).as_context(line_numbers=True)
            pack = self.components.retriever.search_base(
                repo, symbol, top_k=3, branch=resolved, include_tests=True)
            return pack.as_context(line_numbers=True) or "(определение не найдено)"
        except Exception:
            log.warning("definition: сбой", exc_info=True)
            return "(определение не найдено)"
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_service.py -q`
Expected: PASS (весь файл).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "refactor(mcp): definition рендерит через as_context (убран дубль формата), include_tests=True"
```

---

### Task 7: MCP-тул `search_codebase` — параметр `include_tests`

**Files:**
- Modify: `reviewer/entrypoints/mcp_server.py` (тул `search_codebase`, текущие строки 109-116)
- Test: `tests/mcp/test_server_tools.py` (обновить `test_search_codebase_tool_forwards_repo`)

**Interfaces:**
- Consumes: `MCPReviewService.search_codebase(repo, query, top_k, branch, include_tests)` (Task 5).
- Produces: MCP-тул `search_codebase(repo, query, top_k=10, branch=None, include_tests=False)` пробрасывает все 5 позиционных аргументов в сервис.

- [ ] **Step 1: Обновить тест (станет падающим)**

В `tests/mcp/test_server_tools.py` заменить ассерт в `test_search_codebase_tool_forwards_repo` (строка 80):

```python
    svc.search_codebase.assert_called_once_with("owner/name", "token verification", 5, None, False)
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_server_tools.py::test_search_codebase_tool_forwards_repo -q`
Expected: FAIL — тул пробрасывает 4 аргумента, ассерт ждёт 5.

- [ ] **Step 3: Реализовать**

В `reviewer/entrypoints/mcp_server.py` заменить тул `search_codebase` (строки 109-116):

```python
    @mcp.tool()
    def search_codebase(repo: str, query: str, top_k: int = 10,
                        branch: str | None = None,
                        include_tests: bool = False) -> str:
        """Hybrid semantic+lexical search over a repo's base code index (no PR session).
        repo is "owner/name" (or "" to use DEFAULT_REPO). branch is a tracked branch
        (REVIEW_BRANCHES); defaults to the primary branch. Results are deduplicated
        (no nested class/method duplicates) and line-numbered for citing path:line
        without a re-Read; test files are excluded unless include_tests=True. Use it
        (e.g. from /solve-task) to find relevant existing code by a free-text formulation."""
        return service.search_codebase(repo, query, top_k, branch, include_tests)
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_server_tools.py tests/mcp/test_server.py -q`
Expected: PASS. Если в `tests/mcp/test_server.py` есть тест на фиксированное число тулов (напр. 18) — он остаётся зелёным (число тулов не изменилось, добавлен только параметр). Если такой тест падает — это сигнал к проверке, но изменений по числу тулов нет.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/entrypoints/mcp_server.py tests/mcp/test_server_tools.py
git commit -m "feat(mcp): параметр include_tests в туле search_codebase"
```

---

### Task 8: Guidance в SKILL.md (`ask` и `solve-task`)

**Files:**
- Modify: `plugin/skills/ask/SKILL.md`
- Modify: `plugin/skills/solve-task/SKILL.md`

**Interfaces:** документация; тестов нет. Не менять смысл pipeline, только усилить guidance под новую выдачу.

- [ ] **Step 1: `ask/SKILL.md` — пропуск графа + цитирование по выдаче**

В `plugin/skills/ask/SKILL.md` в шаге «3. Expand (only as needed)» добавить в начало абзаца предложение о пропуске графа по умолчанию:

```markdown
3. **Expand (only as needed).** For an architectural / "how does X work" question, DEFAULT to
   skipping the graph tools (`related_symbols` / `callers` / `definition`) — the hybrid search
   usually suffices; CLAUDE.md / README are cheap priors to consult first. Only when the answer
   genuinely needs call relationships, for the symbols most relevant to the question, call
   `related_symbols` / `callers` / `definition` to follow the graph. Do NOT expand everything —
   only what the answer requires. Stop once you can answer.
```

В шаге «4. Confirm source» смягчить требование Read под номера строк:

```markdown
4. **Confirm source.** `search_codebase` snippets are line-numbered, so when the returned snippet
   already shows the exact code you cite, you may cite `path:line` directly from the tool output —
   a separate `Read` is not required for grounding. Use `Read` only when the snippet was truncated
   (`[...truncated]`) or you need surrounding context. Never cite a `path:line` not present in any
   tool output.
```

В «Grounding contract (hard rule)» добавить уточнение последним предложением:

```markdown
A line-numbered `search_codebase` snippet that contains the cited code counts as grounding — an
extra `Read` of the same lines is redundant.
```

- [ ] **Step 2: `solve-task/SKILL.md` — граф оставить, номера строк**

В `plugin/skills/solve-task/SKILL.md` в блоке «Deepen via the code graph» усилить указание про экономию (после фразы про раскрытие только центральных символов, строка ~48):

```markdown
     `search_codebase` now returns deduplicated, line-numbered, test-free snippets — keep using the
     graph tools for blast radius, but expand only the few symbols central to the task, and cite
     `path:line` from the line-numbered snippets directly (no re-Read needed for grounding).
```

- [ ] **Step 3: Проверка — sanity-чтение**

Run: `.venv/bin/ruff check plugin 2>/dev/null; echo "markdown — линта нет, проверяем глазами"`
Перечитать оба файла: pipeline-шаги не сломаны, формулировки на английском (тело скилла), смысл сохранён.

- [ ] **Step 4: Коммит**

```bash
git add plugin/skills/ask/SKILL.md plugin/skills/solve-task/SKILL.md
git commit -m "docs(skill): guidance ask/solve-task под дедуп+номера строк search_codebase"
```

---

### Task 9: Финальная проверка + замер (критерий №4)

**Files:** нет изменений кода; результат замера — в описание PR и (опционально) в спек.

**Interfaces:** verification gate всей задачи.

- [ ] **Step 1: Полный прогон unit-тестов + линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer tests`
Expected: все unit-тесты зелёные; ruff без новых замечаний по затронутым файлам (учесть, что repo-wide ruff может быть не чист — сверять только свои изменения, см. память refactor-verification-gotchas).

- [ ] **Step 2: Замер before/after (требует поднятых ParadeDB/Neo4j + ключ Voyage + построенный индекс)**

Предусловие: `docker compose up -d`, `.env` с ключами, `reviewer index . --ref dev` выполнен.

«After» (текущая ветка) — длина выдачи, которую видит агент:

```bash
.venv/bin/python -c "
from reviewer.config.settings import Settings
from reviewer.app import build_components
c = build_components(Settings())
pack = c.retriever.search_base('mimfort/rag_for_git', 'как работает RAG', top_k=10, branch='dev')
out = pack.as_context(line_numbers=True)
print('after chars=', len(out), ' ~tokens=', len(out)//4, ' items=', len(pack.items))
"
```

«Before» (база сравнения) — на ветке `dev` (до изменений), той же командой, но `pack.as_context()` (старая сигнатура без `line_numbers`):

```bash
git stash 2>/dev/null; git switch dev
.venv/bin/python -c "
from reviewer.config.settings import Settings
from reviewer.app import build_components
c = build_components(Settings())
pack = c.retriever.search_base('mimfort/rag_for_git', 'как работает RAG', top_k=10, branch='dev')
out = pack.as_context()
print('before chars=', len(out), ' ~tokens=', len(out)//4, ' items=', len(pack.items))
"
git switch feat/pri-138-search-codebase-output; git stash pop 2>/dev/null || true
```

Повторить на 2-м типовом запросе (напр. `как устроена индексация`). Зафиксировать числа.

- [ ] **Step 3: Записать результат в описание PR**

В тело PR добавить таблицу before/after (chars/≈tokens/items) по двум запросам и вывод: выдача короче при сохранённом recall (ключевые символы ядра присутствуют в обоих прогонах). Если по какому-то запросу выдача НЕ короче — зафиксировать наблюдение и причину (напр. мало дублей/тестов в топе), это допустимо: механика доказана юнит-тестами.

- [ ] **Step 4: Финальный коммит/PR**

```bash
git push -u origin feat/pri-138-search-codebase-output
```

Создать PR в `dev` с описанием и замером. Ссылка на задачу: PRI-138.

---

## Self-Review (выполнено автором плана)

**Spec coverage:**
- Дедуп вложенных чанков → Task 1 (+ подключение Task 4). ✓
- Построчные номера в `as_context` (opt-in, дефолт без изменений) → Task 3. ✓
- `definition` через `as_context`, без дубля формата `:418` → Task 6. ✓
- `include_tests=False` по умолчанию + предикат + проброс через MCP-тул → Tasks 2, 4, 5, 7. ✓
- `definition` тесты не режет (`include_tests=True`) → Task 6. ✓
- Guidance `ask` (пропуск графа, цитирование по выдаче) и `solve-task` (граф оставить, номера) → Task 8. ✓
- Замер крит.4 (юниты + ручной before/after) → Tasks 1-7 (юниты) + Task 9 (замер). ✓
- Инвариант «PR-путь не трогаем» → дефолт `line_numbers=False`, дедуп/фильтр только в `search_base`; `code_tools.py` не в списке изменений. ✓

**Placeholder scan:** плейсхолдеров нет; в каждом code-шаге приведён реальный код.

**Type consistency:** имена сквозные — `_dedupe_overlapping`, `_is_test_path`, `as_context(line_numbers=…)`, `search_base(..., include_tests=…)`, `search_codebase(..., include_tests=…)` — совпадают между задачами 1→4→5→7 и 3→5→6.
