# read_file skeleton-режим (PRI-154) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить `skeleton=True` в MCP-тул `read_file` — отдавать AST-скелет (сигнатуры def/class + первая строка docstring) вместо полного тела, ради экономии токенов на навигации.

**Architecture:** Чистая функция `python_skeleton(source: bytes) -> list[int]` в `chunker.py` (переиспользует tree-sitter `_PARSER`) возвращает номера строк скелета; `read_file` в `code_tools.py` рендерит их в формате `N|код` с капом 400; параметр `skeleton` протягивается через `service.read_file` и FastMCP-обёртку `mcp_server.read_file`.

**Tech Stack:** Python 3.11+, tree-sitter-python, LangChain StructuredTool, FastMCP, pytest.

## Global Constraints

- Язык кода/докстрингов/сообщений — **русский** (стиль проекта).
- Внешних зависимостей не добавлять; переиспользовать `_PARSER`/`_PY`/`_DEF_TYPES` из `reviewer/index/chunker.py`.
- Полный режим `read_file` (`skeleton=False`) **не меняется** — существующие тесты зелёные.
- ruff line-length 100, target py311.
- Коммиты: Conventional Commits на русском, **без self-attribution**.

---

### Task 1: `python_skeleton` в chunker.py

**Files:**
- Modify: `reviewer/index/chunker.py` (добавить функцию после `chunk_python`)
- Test: `tests/index/test_chunker.py`

**Interfaces:**
- Produces: `python_skeleton(source: bytes) -> list[int]` — отсортированный уникальный список 1-based номеров строк скелета (модульный docstring 1-я строка, декораторы+заголовки def/class до `:`, 1-я строка docstring каждого определения). `[]` если определений нет.

- [ ] **Step 1: Написать падающие тесты**

В `tests/index/test_chunker.py` добавить (импорт расширить: `from reviewer.index.chunker import chunk_python, python_skeleton`):

```python
SKEL_SRC = b'''\
"""Module doc.
more."""
import os

@dec
def top(a,
        b):
    """Top doc."""
    return a + b

class A:
    """Class A."""
    def method(self):
        x = 1
        return x
'''

def test_skeleton_includes_signatures_and_docstrings_not_bodies():
    nums = python_skeleton(SKEL_SRC)
    lines = SKEL_SRC.decode().splitlines()
    picked = [lines[n - 1] for n in nums]
    assert any('"""Module doc.' in v for v in picked)      # модульный docstring (1-я строка)
    assert any("@dec" in v for v in picked)                # декоратор
    assert any("def top(a," in v for v in picked)          # многострочная сигнатура — строка 1
    assert any("b):" in v for v in picked)                 # и строка 2 (до ':')
    assert any('"""Top doc."""' in v for v in picked)      # docstring функции
    assert any("class A:" in v for v in picked)
    assert any('"""Class A."""' in v for v in picked)
    assert any("def method(self):" in v for v in picked)
    assert all("return a + b" not in v for v in picked)    # тела НЕ включены
    assert all("x = 1" not in v for v in picked)
    assert all("return x" not in v for v in picked)
    assert "more." not in "\n".join(picked)                # только 1-я строка модульного docstring

def test_skeleton_empty_for_source_without_definitions():
    assert python_skeleton(b"import os\nX = 1\nprint(X)\n") == []

def test_skeleton_does_not_crash_on_syntax_error():
    assert isinstance(python_skeleton(b"def f(:\n  pass\n"), list)
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/index/test_chunker.py -q`
Expected: FAIL (`ImportError: cannot import name 'python_skeleton'`).

- [ ] **Step 3: Реализовать `python_skeleton`**

В `reviewer/index/chunker.py` добавить после `chunk_python`:

```python
def python_skeleton(source: bytes) -> list[int]:
    """AST-скелет файла: 1-based номера строк заголовков def/class (с декораторами,
    многострочные сигнатуры — до ':'), первой строки docstring каждого определения и
    модульного docstring. Рендер (N|код) и кап — на стороне вызывающего (read_file).
    Отсортированный уникальный список; [] если определений нет. Не падает на битом коде."""
    tree = _PARSER.parse(source)
    lines: set[int] = set()

    def first_docstring_line(body) -> None:
        if body is None:
            return
        for stmt in body.children:
            if not stmt.is_named or stmt.type == "comment":
                continue
            if (stmt.type == "expression_statement" and stmt.children
                    and stmt.children[0].type == "string"):
                lines.add(stmt.children[0].start_point[0] + 1)
            return  # первый значимый statement рассмотрен — дальше docstring быть не может

    first_docstring_line(tree.root_node)  # модульный docstring

    def visit(node) -> None:
        for child in node.children:
            defn, outer = child, child
            if child.type == "decorated_definition":
                defn = child.child_by_field_name("definition")
                outer = child
            if defn is not None and defn.type in _DEF_TYPES:
                colon_line = defn.start_point[0]
                for c in defn.children:
                    if c.type == ":":
                        colon_line = c.start_point[0]
                        break
                for ln in range(outer.start_point[0], colon_line + 1):
                    lines.add(ln + 1)
                body = defn.child_by_field_name("body")
                first_docstring_line(body)
                if body is not None:
                    visit(body)
            else:
                visit(child)

    visit(tree.root_node)
    return sorted(lines)
```

- [ ] **Step 4: Запустить — убедиться, что зелено**

Run: `.venv/bin/pytest tests/index/test_chunker.py -q`
Expected: PASS (все тесты chunker).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/index/chunker.py tests/index/test_chunker.py
git commit -m "feat(index): python_skeleton — AST-скелет файла (заголовки def/class + docstring) для PRI-154"
```

---

### Task 2: `skeleton` в `read_file` (code_tools.py)

**Files:**
- Modify: `reviewer/tools/code_tools.py` (импорт + функция `read_file`)
- Test: `tests/tools/test_code_tools.py`

**Interfaces:**
- Consumes: `python_skeleton(source: bytes) -> list[int]` (Task 1).
- Produces: `read_file(path, start=1, end=400, skeleton=False) -> str` — при `skeleton=True` рендерит скелет (`N|код`), `start/end` игнорируются, кап 400; при отсутствии определений — фолбэк на обычный срез с заметкой-префиксом.

- [ ] **Step 1: Написать падающие тесты**

В `tests/tools/test_code_tools.py` добавить:

```python
def test_read_file_skeleton_returns_signatures_not_bodies():
    src = ('class A:\n'
           '    """Doc A."""\n'
           '    def m(self):\n'
           '        secret = 42\n'
           '        return secret\n')
    tools = {t.name: t for t in make_tools(_rich_ctx(read_file_fn=lambda p: src))}
    out = tools["read_file"].invoke({"path": "a.py", "skeleton": True})
    assert "class A:" in out and '"""Doc A."""' in out and "def m(self):" in out
    assert "secret = 42" not in out and "return secret" not in out

def test_read_file_skeleton_fallback_when_no_definitions():
    src = "import os\nX = 1\n"
    tools = {t.name: t for t in make_tools(_rich_ctx(read_file_fn=lambda p: src))}
    out = tools["read_file"].invoke({"path": "a.py", "skeleton": True})
    assert "нет определений для скелета" in out
    assert "1|import os" in out and "2|X = 1" in out

def test_read_file_full_mode_unchanged_with_skeleton_false():
    tools = {t.name: t for t in make_tools(_rich_ctx())}   # a.py = "l1\nl2\nl3"
    out = tools["read_file"].invoke({"path": "a.py", "start": 1, "end": 2})
    assert "1|l1" in out and "2|l2" in out and "3|l3" not in out
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tools/test_code_tools.py -q`
Expected: FAIL (skeleton-тесты: `read_file()` не принимает `skeleton` / нет ожидаемого вывода).

- [ ] **Step 3: Реализовать**

В `reviewer/tools/code_tools.py` добавить импорт вверху (рядом с `from reviewer.index.refs import base_ref`):

```python
from reviewer.index.chunker import python_skeleton
```

Заменить тело `read_file` (строки ~79-100). Сигнатура и начало (проверки) — как раньше, добавить ветку skeleton перед обычным срезом:

```python
    def read_file(path: str, start: int = 1, end: int = 400, skeleton: bool = False) -> str:
        """Исходник файла на head-ревизии PR. По умолчанию строки [start..end] с номерами (N|код),
        окно ≤400 строк. При skeleton=True — AST-скелет файла (сигнатуры def/class + 1-я строка
        docstring) вместо тел, для первичной ориентации; start/end игнорируются, полное тело —
        последующим read_file(path, start, end). Меньше токенов на навигацию."""
        if ctx.read_file_fn is None:
            return "(чтение файлов недоступно)"
        src = ctx.read_file_fn(path)
        if src is None:
            return f"(файл не найден: {path})"
        lines = src.splitlines()
        if not lines:
            return "(файл пуст)"
        skel_note = ""
        if skeleton:
            nums = [n for n in python_skeleton(src.encode("utf-8")) if 1 <= n <= len(lines)]
            if nums:
                capped = len(nums) > 400
                nums = nums[:400]
                out = "\n".join(f"{n}|{lines[n - 1]}" for n in nums)
                return out + "\n(…усечено)" if capped else out
            skel_note = "(нет определений для скелета — полный фрагмент)\n"
        s = max(1, start)
        if s > len(lines):
            return f"(нет строки {s}; в файле {len(lines)} строк)"
        e = min(len(lines), end)
        capped = (e - s + 1 > 400)
        if capped:
            e = s + 399
        body = "\n".join(f"{i}|{lines[i - 1]}" for i in range(s, e + 1))
        if capped:
            body += "\n(…усечено)"
        return skel_note + body
```

- [ ] **Step 4: Запустить — убедиться, что зелено**

Run: `.venv/bin/pytest tests/tools/test_code_tools.py -q`
Expected: PASS (старые read_file-тесты + новые skeleton-тесты).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tools/code_tools.py tests/tools/test_code_tools.py
git commit -m "feat(tools): read_file(skeleton=True) — AST-скелет файла для экономии токенов (PRI-154)"
```

---

### Task 3: Проброс `skeleton` через service + mcp_server

**Files:**
- Modify: `reviewer/mcp/service.py:283-288` (`read_file`)
- Modify: `reviewer/entrypoints/mcp_server.py:44-48` (FastMCP-обёртка `read_file`)
- Test: `tests/mcp/test_service.py:254`

**Interfaces:**
- Consumes: `read_file(..., skeleton=False)` тула (Task 2).
- Produces: `service.read_file(repo, pr, path, start=1, end=400, skeleton=False) -> str`; FastMCP `read_file(repo, pr, path, start=1, end=400, skeleton=False) -> str`.

- [ ] **Step 1: Расширить guard-тест делегата**

В `tests/mcp/test_service.py` в `test_search_tools_delegate_to_make_tools` после строки 254 добавить:

```python
    assert isinstance(svc.read_file("o/r", 7, "a.py", 1, 10, skeleton=True), str)
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_service.py::test_search_tools_delegate_to_make_tools -q`
Expected: FAIL (`read_file() got an unexpected keyword argument 'skeleton'`).

- [ ] **Step 3: Реализовать проброс**

В `reviewer/mcp/service.py` заменить `read_file`:

```python
    def read_file(self, repo: str, pr: int, path: str, start: int = 1, end: int = 400,
                  skeleton: bool = False) -> str:
        """Исходник файла на head-ревизии PR, строки [start..end].

        При skeleton=True — AST-скелет (сигнатуры def/class + 1-я строка docstring),
        start/end игнорируются. Дефолты start/end синхронизированы с code_tools.read_file.
        """
        return self._invoke_tool(repo, pr, "read_file",
                                 {"path": path, "start": start, "end": end, "skeleton": skeleton})
```

В `reviewer/entrypoints/mcp_server.py` заменить обёртку `read_file`:

```python
    @mcp.tool()
    def read_file(repo: str, pr: int, path: str, start: int = 1, end: int = 400,
                  skeleton: bool = False) -> str:
        """Read source lines of a file at the PR head revision.
        start/end are 1-based inclusive line numbers (full mode).
        skeleton=True returns an AST skeleton (def/class signatures + first docstring line)
        instead of bodies — compact orientation; fetch a full body afterwards with a range."""
        return service.read_file(repo, pr, path, start, end, skeleton)
```

- [ ] **Step 4: Запустить — убедиться, что зелено**

Run: `.venv/bin/pytest tests/mcp/test_service.py -q`
Expected: PASS.

- [ ] **Step 5: Полный прогон unit + ruff**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer/index/chunker.py reviewer/tools/code_tools.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py`
Expected: все unit зелёные; ruff чист по затронутым файлам.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_service.py
git commit -m "feat(mcp): проброс skeleton в read_file через service + FastMCP-обёртку (PRI-154)"
```

---

## Self-Review

- **Spec coverage:** `python_skeleton` (Task 1) ↔ компонент 1 спеки; `read_file(skeleton)` + фолбэк + кап (Task 2) ↔ компонент 2; проброс service+mcp_server (Task 3) ↔ компоненты 3-4. Тесты покрывают сигнатуры/docstring/тела/фолбэк/битый код/проброс. ✓
- **Placeholder scan:** плейсхолдеров нет, весь код приведён. ✓
- **Type consistency:** `python_skeleton(bytes)->list[int]` едина во всех тасках; `skeleton: bool=False` одинаков в code_tools/service/mcp_server. ✓
