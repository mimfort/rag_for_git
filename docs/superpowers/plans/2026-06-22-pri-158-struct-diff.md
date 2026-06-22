# tree-sitter структурный diff (PRI-158) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Подмешивать в PR-units компактную структурную сводку изменений символов (изменённые сигнатуры / добавленные / удалённые), чтобы агент-ревьюер точнее оценивал контракт/blast-radius при меньшем числе навигационных токенов.

**Architecture:** Новый чистый модуль `reviewer/index/struct_diff.py` чанкает base- и head-исходник файла через существующий `chunk_python` (tree-sitter), сопоставляет символы по `fqn` и классифицирует add/remove/signature-change (телесные правки не репортятся). `ReviewService.prepare` считает сводку для изменённых на месте файлов (fail-soft) и кладёт в `ReviewUnit.structural_summary`; `_prepared_payload` пробрасывает её в payload analyze-этапа; промпт `analyze-prompt.md` учит субагента её использовать.

**Tech Stack:** Python 3.11–3.13, tree-sitter (`tree_sitter_python`), pytest, ruff, FastMCP.

## Global Constraints

- Python 3.11–3.13; ruff line-length 100, target py311 (`.venv/bin/ruff check .`).
- Язык кода — **русский**: комментарии, докстринги, сообщения. Сохранять стиль.
- Коммиты: **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude). Conventional Commits на русском (`feat(...)`, `refactor(...)`, `test(...)`, `docs(...)`).
- TDD: сначала падающий тест, потом минимальная реализация.
- Тесты: `.venv/bin/pytest -q` (integration по умолчанию исключены).
- Работаем в ветке `feat/struct-diff-pri-158` (спека уже закоммичена туда).
- Ключевой инвариант: `node_id = "path#fqn"`; `Chunk.symbol_fqn` — ключ сопоставления символов; `chunk_python(path, source: bytes)` возвращает `list[Chunk]` с полями `symbol_fqn`, `kind` (`class|method|function`), `start_line`, `end_line`, `text`.
- Сводка **строго дополняет** raw-patch и никогда его не заменяет.

---

## File Structure

| Файл | Ответственность |
|---|---|
| `reviewer/index/struct_diff.py` (new) | `extract_signature` (перенос), `SymbolChange`, `diff_symbols`, `format_struct_summary` |
| `reviewer/tools/impact.py` (mod) | ре-экспорт `extract_signature` из `index.struct_diff` |
| `reviewer/agent/state.py` (mod) | поле `structural_summary: str = ""` в `ReviewUnit` |
| `reviewer/services/review_service.py` (mod) | вычисление сводки в `prepare` (fail-soft) |
| `reviewer/mcp/service.py` (mod) | проброс `structural_summary` в `_prepared_payload` |
| `plugin/skills/review-pr/references/analyze-prompt.md` (mod) | инструкция использовать сводку |
| `tests/index/test_struct_diff.py` (new) | unit-тесты модуля |
| `tests/tools/test_impact.py` (existing) | остаются зелёными через ре-экспорт |
| `tests/services/test_review_service.py` (mod) | сводка устанавливается в units |
| `tests/mcp/test_service.py` (mod) | сводка пробрасывается в payload |

---

### Task 1: Перенести `extract_signature` в `reviewer/index/struct_diff.py` + ре-экспорт

Чистый рефакторинг: один источник истины, корректные слои (`index/` ниже `tools/`). Поведение `extract_signature` не меняется; существующие тесты `test_impact.py` должны остаться зелёными.

**Files:**
- Create: `reviewer/index/struct_diff.py`
- Modify: `reviewer/tools/impact.py:1-37` (удалить `import re`, `_DEF_RE`, `_WS_RE`, `def extract_signature`; добавить импорт)
- Test: `tests/index/test_struct_diff.py` (new), `tests/tools/test_impact.py` (existing)

**Interfaces:**
- Produces: `extract_signature(node_text: str) -> str | None` в `reviewer.index.struct_diff`; ре-экспорт из `reviewer.tools.impact` (старый импорт продолжает работать).

- [ ] **Step 1: Написать падающий тест на импорт из нового модуля**

В новый файл `tests/index/test_struct_diff.py`:

```python
from reviewer.index.struct_diff import extract_signature


def test_extract_signature_reexported_and_works():
    assert extract_signature("def f(a, b):\n    return a") == "def f(a, b):"
    assert extract_signature("class A(B, C):\n    pass") == "class A(B, C):"
    assert extract_signature("x = 1") is None
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/index/test_struct_diff.py -q`
Expected: FAIL с `ModuleNotFoundError: No module named 'reviewer.index.struct_diff'`.

- [ ] **Step 3: Создать `reviewer/index/struct_diff.py` с перенесённым `extract_signature`**

```python
"""Структурный diff символов: сопоставление сигнатур/символов до и после (tree-sitter).

Подаёт агенту компактную сводку контрактных изменений рядом с сырым unified-diff.
"""
from __future__ import annotations

import re

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

- [ ] **Step 4: Убрать дубликат из `reviewer/tools/impact.py` и ре-экспортировать**

В `reviewer/tools/impact.py` удалить строки `import re` (строка 5), блок `_DEF_RE`/`_WS_RE` (строки 9-10) и всю функцию `extract_signature` (строки 13-37). Вместо них в шапке импортов добавить:

```python
from reviewer.index.refs import base_ref
from reviewer.index.struct_diff import extract_signature  # ре-экспорт (обратная совместимость)
```

(`from __future__ import annotations` и `from dataclasses import dataclass, field` оставить как есть. `import re` удалить — после переноса он в `impact.py` не используется.)

- [ ] **Step 5: Запустить тесты нового модуля и impact**

Run: `.venv/bin/pytest tests/index/test_struct_diff.py tests/tools/test_impact.py -q`
Expected: PASS (включая существующие `test_extract_signature_*` и `test_compute_impact_*`).

- [ ] **Step 6: Линт и коммит**

Run: `.venv/bin/ruff check reviewer/index/struct_diff.py reviewer/tools/impact.py`
Expected: без ошибок (в частности, нет неиспользуемого `import re` в `impact.py`).

```bash
git add reviewer/index/struct_diff.py reviewer/tools/impact.py tests/index/test_struct_diff.py
git commit -m "refactor(index): вынести extract_signature в index/struct_diff с ре-экспортом (PRI-158)"
```

---

### Task 2: `SymbolChange` + `diff_symbols`

Сопоставление символов base/head через `chunk_python` и классификация add/remove/signature-change. Телесные правки (сигнатура та же) не репортятся. Fail-soft: не бросает исключений.

**Files:**
- Modify: `reviewer/index/struct_diff.py`
- Test: `tests/index/test_struct_diff.py`

**Interfaces:**
- Consumes: `chunk_python(path: str, source: bytes) -> list[Chunk]` (`reviewer.index.chunker`); `extract_signature` (Task 1).
- Produces:
  - `@dataclass SymbolChange{kind: str, fqn: str, symbol_kind: str, old_sig: str | None, new_sig: str | None, line: int | None}`; `kind ∈ {"signature_changed","added","removed"}`.
  - `diff_symbols(path: str, base_source: bytes | None, head_source: bytes) -> list[SymbolChange]`.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/index/test_struct_diff.py`:

```python
from reviewer.index.struct_diff import SymbolChange, diff_symbols


def _kinds(changes):
    return {(c.kind, c.fqn) for c in changes}


def test_diff_signature_changed():
    base = b"def foo(a):\n    return a\n"
    head = b"def foo(a, b):\n    return a\n"
    changes = diff_symbols("m.py", base, head)
    assert len(changes) == 1
    c = changes[0]
    assert c.kind == "signature_changed"
    assert c.fqn == "foo"
    assert c.old_sig == "def foo(a):"
    assert c.new_sig == "def foo(a, b):"


def test_diff_added_and_removed():
    base = b"def gone():\n    pass\n"
    head = b"def fresh():\n    pass\n"
    changes = diff_symbols("m.py", base, head)
    assert _kinds(changes) == {("added", "fresh"), ("removed", "gone")}


def test_diff_body_only_change_not_reported():
    base = b"def foo(a):\n    return a\n"
    head = b"def foo(a):\n    return a + 1\n"
    assert diff_symbols("m.py", base, head) == []


def test_diff_method_and_class_kinds():
    base = b"class A:\n    def m(self):\n        return 1\n"
    head = b"class A:\n    def m(self, x):\n        return 1\n"
    changes = diff_symbols("m.py", base, head)
    assert _kinds(changes) == {("signature_changed", "A.m")}
    assert changes[0].symbol_kind == "method"


def test_diff_base_none_means_all_added():
    head = b"def foo(a):\n    pass\n\ndef bar():\n    pass\n"
    changes = diff_symbols("m.py", None, head)
    assert _kinds(changes) == {("added", "foo"), ("added", "bar")}


def test_diff_broken_source_fail_soft():
    # битый/неполный исходник не должен бросать исключение
    assert diff_symbols("m.py", b"def (:\n", b"def foo(:\n") == [] or True
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/index/test_struct_diff.py -q`
Expected: FAIL с `ImportError: cannot import name 'SymbolChange'`.

- [ ] **Step 3: Реализовать `SymbolChange` и `diff_symbols`**

Добавить в `reviewer/index/struct_diff.py` (импорты — в шапку файла, классы/функции — ниже `extract_signature`):

```python
from dataclasses import dataclass

from reviewer.index.chunker import chunk_python
```

```python
@dataclass
class SymbolChange:
    """Одно структурное изменение символа между base и head."""

    kind: str               # "signature_changed" | "added" | "removed"
    fqn: str                # напр. "A.m" или "foo"
    symbol_kind: str        # class | method | function
    old_sig: str | None     # заголовок до (для removed/signature_changed)
    new_sig: str | None     # заголовок после (для added/signature_changed)
    line: int | None        # head-строка для added/changed; base-строка для removed


def _symbol_map(path: str, source: bytes | None) -> dict:
    """fqn -> Chunk по исходнику (tree-sitter). Fail-soft: {} при пустом/битом вводе."""
    if not source:
        return {}
    try:
        return {ch.symbol_fqn: ch for ch in chunk_python(path, source)}
    except Exception:
        return {}


def diff_symbols(
    path: str, base_source: bytes | None, head_source: bytes
) -> list[SymbolChange]:
    """Структурный diff символов файла: add / remove / signature-change.

    Сопоставляет символы base и head по fqn. Чисто телесные правки (сигнатура
    не менялась) НЕ репортятся — это и даёт компактность. base_source=None →
    все символы head как added (политику пропуска added-файлов реализует
    вызывающий). Не бросает исключений.
    """
    base = _symbol_map(path, base_source)
    head = _symbol_map(path, head_source)
    changes: list[SymbolChange] = []

    for fqn, old in base.items():
        new = head.get(fqn)
        if new is None:
            changes.append(SymbolChange(
                "removed", fqn, old.kind, extract_signature(old.text), None,
                old.start_line))
            continue
        old_sig, new_sig = extract_signature(old.text), extract_signature(new.text)
        if old_sig and new_sig and old_sig != new_sig:
            changes.append(SymbolChange(
                "signature_changed", fqn, new.kind, old_sig, new_sig,
                new.start_line))

    for fqn, new in head.items():
        if fqn not in base:
            changes.append(SymbolChange(
                "added", fqn, new.kind, None, extract_signature(new.text),
                new.start_line))

    return changes
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/index/test_struct_diff.py -q`
Expected: PASS.

- [ ] **Step 5: Линт и коммит**

Run: `.venv/bin/ruff check reviewer/index/struct_diff.py`
Expected: без ошибок.

```bash
git add reviewer/index/struct_diff.py tests/index/test_struct_diff.py
git commit -m "feat(index): diff_symbols — структурный diff символов на tree-sitter (PRI-158)"
```

---

### Task 3: `format_struct_summary`

Рендер списка `SymbolChange` в компактный текстовый блок для агента. Пустой ввод → `""`. Кап ~40 строк.

**Files:**
- Modify: `reviewer/index/struct_diff.py`
- Test: `tests/index/test_struct_diff.py`

**Interfaces:**
- Consumes: `list[SymbolChange]` (Task 2).
- Produces: `format_struct_summary(changes: list[SymbolChange]) -> str` (`""` если изменений нет).

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/index/test_struct_diff.py`:

```python
from reviewer.index.struct_diff import format_struct_summary


def test_format_empty_returns_empty_string():
    assert format_struct_summary([]) == ""


def test_format_renders_all_kinds_in_order():
    changes = [
        SymbolChange("removed", "old_fn", "function", "def old_fn():", None, 5),
        SymbolChange("added", "A.new_m", "method", None, "def new_m(self):", 12),
        SymbolChange("signature_changed", "foo", "function",
                     "def foo(a):", "def foo(a, b):", 1),
    ]
    out = format_struct_summary(changes)
    assert out.startswith("Структурный diff:")
    # порядок: signature_changed -> added -> removed
    i_sig = out.index("foo")
    i_add = out.index("A.new_m")
    i_rem = out.index("old_fn")
    assert i_sig < i_add < i_rem
    assert "было: def foo(a):" in out
    assert "стало: def foo(a, b):" in out
    assert "(method)" in out


def test_format_caps_long_lists():
    changes = [
        SymbolChange("added", f"f{i}", "function", None, f"def f{i}():", i)
        for i in range(50)
    ]
    out = format_struct_summary(changes)
    assert "(…ещё 10)" in out
    assert out.count("def f") == 40
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/index/test_struct_diff.py -q`
Expected: FAIL с `ImportError: cannot import name 'format_struct_summary'`.

- [ ] **Step 3: Реализовать `format_struct_summary`**

Добавить в конец `reviewer/index/struct_diff.py`:

```python
_ORDER = {"signature_changed": 0, "added": 1, "removed": 2}
_MAX_LINES = 40


def format_struct_summary(changes: list[SymbolChange]) -> str:
    """Компактный текстовый блок структурного diff для агента; "" если изменений нет.

    Порядок: изменённые сигнатуры → добавленные → удалённые. При >40 строк —
    хвостовая пометка «(…ещё N)».
    """
    if not changes:
        return ""
    ordered = sorted(changes, key=lambda c: (_ORDER.get(c.kind, 9), c.fqn))
    rows: list[str] = []
    for c in ordered:
        if c.kind == "signature_changed":
            rows.append(f"  ~ сигнатура  {c.fqn}  было: {c.old_sig}  стало: {c.new_sig}")
        elif c.kind == "added":
            rows.append(f"  + добавлен   {c.fqn}  ({c.symbol_kind})")
        else:
            rows.append(f"  - удалён     {c.fqn}  ({c.symbol_kind})")
    extra = len(rows) - _MAX_LINES
    if extra > 0:
        rows = rows[:_MAX_LINES]
    out = "Структурный diff:\n" + "\n".join(rows)
    if extra > 0:
        out += f"\n  (…ещё {extra})"
    return out
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/index/test_struct_diff.py -q`
Expected: PASS.

- [ ] **Step 5: Линт и коммит**

Run: `.venv/bin/ruff check reviewer/index/struct_diff.py`
Expected: без ошибок.

```bash
git add reviewer/index/struct_diff.py tests/index/test_struct_diff.py
git commit -m "feat(index): format_struct_summary — компактный рендер структурного diff (PRI-158)"
```

---

### Task 4: Поле `ReviewUnit.structural_summary` + вычисление в `prepare`

Добавить поле в `ReviewUnit`; в `prepare` считать сводку для файлов `status == "modified"` (added/renamed — пропуск), fail-soft.

**Files:**
- Modify: `reviewer/agent/state.py:5-10`
- Modify: `reviewer/services/review_service.py` (импорт + хелпер + цикл units, строки 22-26 и 234-242)
- Test: `tests/services/test_review_service.py`

**Interfaces:**
- Consumes: `diff_symbols`, `format_struct_summary` (Tasks 2-3); `vcs.get_file_at_ref(path: str, ref: str) -> str | None`; `prq.base_sha`.
- Produces: `ReviewUnit.structural_summary: str` (дефолт `""`); заполняется в `PreparedReview.units`.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/services/test_review_service.py`:

```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_attaches_structural_summary_for_modified_file(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Для изменённого на месте файла со сменой сигнатуры prepare кладёт
    непустой structural_summary в юнит."""
    vcs = _vcs_with_files([_changed("a.py", status="modified")])
    vcs.get_pull_request.return_value = PullRequest(
        number=1, base_sha="base123", head_sha="head456", base_ref="main",
        title="t", body="", draft=False,
    )

    def _read(path: str, ref: str) -> str | None:
        if path == ".review.yml":
            return None
        return "def foo(a, b):\n    return a" if ref == "head456" else "def foo(a):\n    return a"
    vcs.get_file_at_ref.side_effect = _read

    service = ReviewService(settings, components)
    prepared = service.prepare("o", "r", 1, vcs_provider=vcs)

    summary = prepared.units[0].structural_summary
    assert "foo" in summary
    assert "сигнатура" in summary


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_no_structural_summary_for_added_file(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Для добавленного файла structural_summary пуст (для нового файла сводка — шум)."""
    vcs = _vcs_with_files([_changed("a.py", status="added")])
    vcs.get_file_at_ref.side_effect = (
        lambda p, r: None if p == ".review.yml" else "def foo(a): pass"
    )

    service = ReviewService(settings, components)
    prepared = service.prepare("o", "r", 1, vcs_provider=vcs)

    assert prepared.units[0].structural_summary == ""
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/services/test_review_service.py -k structural -q`
Expected: FAIL — `ReviewUnit.__init__() got an unexpected keyword argument 'structural_summary'` (или `AttributeError`/`TypeError`).

- [ ] **Step 3: Добавить поле в `ReviewUnit`**

В `reviewer/agent/state.py` дополнить датакласс:

```python
@dataclass
class ReviewUnit:
    path: str
    node_ids: list[str]
    changed_text: str
    new_source: str = ""        # полная новая версия файла (для точных fix-диапазонов)
    structural_summary: str = ""  # компактная сводка структурных изменений символов
```

- [ ] **Step 4: Добавить импорт и хелпер в `review_service.py`**

В `reviewer/services/review_service.py` рядом с импортом chunker (строка 22) добавить:

```python
from reviewer.index.chunker import chunk_python
from reviewer.index.struct_diff import diff_symbols, format_struct_summary
```

Рядом с `_hunk_count`/`_file_importance_key` (после строки 48) добавить модульный хелпер:

```python
def _structural_summary(vcs, path: str, status: str, base_sha: str, head_src: str) -> str:
    """Компактная структурная сводка изменений символов файла (fail-soft).

    Только для изменённых на месте файлов (modified); added/renamed → "".
    Любой сбой (base не дотянулся, tree-sitter упал) → "" — никогда не валит prepare.
    """
    if status != "modified":
        return ""
    try:
        base_src = vcs.get_file_at_ref(path, base_sha)
        if not base_src:
            return ""
        changes = diff_symbols(path, base_src.encode(), head_src.encode())
        return format_struct_summary(changes)
    except Exception:
        log.warning("Не удалось построить структурный diff для %s", path, exc_info=True)
        return ""
```

- [ ] **Step 5: Заполнить `structural_summary` в цикле units**

В `reviewer/services/review_service.py` заменить тело цикла сборки units (строки 234-242) на:

```python
            units: list[ReviewUnit] = []
            for f in selected_files:
                src = head_sources.get(f.path)
                if not src:
                    continue
                node_ids = [ch.node_id for ch in chunk_python(f.path, src.encode())]
                summary = _structural_summary(
                    vcs, f.path, f.status, prq.base_sha, src)
                units.append(
                    ReviewUnit(f.path, node_ids, f.patch or "", new_source=src,
                               structural_summary=summary)
                )
```

- [ ] **Step 6: Запустить тесты — убедиться, что проходят (и не сломаны старые)**

Run: `.venv/bin/pytest tests/services/test_review_service.py -q`
Expected: PASS (новые `*structural*` и все старые тесты `test_review_service.py`).

- [ ] **Step 7: Линт и коммит**

Run: `.venv/bin/ruff check reviewer/agent/state.py reviewer/services/review_service.py`
Expected: без ошибок.

```bash
git add reviewer/agent/state.py reviewer/services/review_service.py tests/services/test_review_service.py
git commit -m "feat(services): структурная сводка изменений символов в units prepare (PRI-158)"
```

---

### Task 5: Проброс `structural_summary` в payload `prepare_review`

Добавить поле в per-unit dict `_prepared_payload` — только когда непусто.

**Files:**
- Modify: `reviewer/mcp/service.py:756-766`
- Test: `tests/mcp/test_service.py`

**Interfaces:**
- Consumes: `PreparedReview.units[i].structural_summary` (Task 4).
- Produces: ключ `units[i]["structural_summary"]` в payload `prepare_review` (присутствует только при непустой сводке).

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/mcp/test_service.py` (после `test_prepare_review_payload_fields`):

```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_payload_includes_structural_summary(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """При смене сигнатуры payload-юнит несёт structural_summary."""
    settings = _settings()
    components = _components()
    vcs = _fake_vcs()

    def _read(path: str, ref: str) -> str | None:
        if path == ".review.yml":
            return None
        return "def foo(a, b): pass" if ref == "head456" else "def foo(a): pass"
    vcs.get_file_at_ref.side_effect = _read

    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    out = svc.prepare_review("o/r", 7)

    unit = out["units"][0]
    assert "structural_summary" in unit
    assert "foo" in unit["structural_summary"]


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_payload_omits_empty_structural_summary(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """Без структурных изменений (base==head) ключ structural_summary отсутствует."""
    svc = _make_mcp_service()  # _fake_vcs отдаёт одинаковый исходник для base и head
    out = svc.prepare_review("o/r", 7)

    assert "structural_summary" not in out["units"][0]
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_service.py -k structural -q`
Expected: FAIL на `test_prepare_review_payload_includes_structural_summary` (`assert "structural_summary" in unit`).

- [ ] **Step 3: Реализовать проброс в `_prepared_payload`**

В `reviewer/mcp/service.py` заменить тело цикла построения `units` (строки 758-766) на:

```python
        units = []
        for u in p.units:
            lines = commentable_lines(p.patches.get(u.path))
            unit = {
                "path": u.path,
                "patch": p.patches.get(u.path),
                "commentable_right": sorted(lines["RIGHT"]),
                "commentable_left": sorted(lines["LEFT"]),
            }
            if u.structural_summary:
                unit["structural_summary"] = u.structural_summary
            units.append(unit)
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_service.py -q`
Expected: PASS (новые + старые тесты payload).

- [ ] **Step 5: Линт и коммит**

Run: `.venv/bin/ruff check reviewer/mcp/service.py`
Expected: без ошибок.

```bash
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "feat(mcp): проброс structural_summary в payload prepare_review (PRI-158)"
```

---

### Task 6: Инструкция в промпте `analyze-prompt.md` + финальная проверка

Научить субагента использовать `structural_summary` для ориентации по контракту/blast-radius. Затем — guard-тесты промптов и полный прогон.

**Files:**
- Modify: `plugin/skills/review-pr/references/analyze-prompt.md` (блок Rules, после строки 6)
- Test: `tests/skills/` (guard), весь набор

**Interfaces:**
- Consumes: payload-юнит с опциональным `structural_summary` (Task 5). Текстовая инструкция; новых include-маркеров не добавляем.

- [ ] **Step 1: Добавить блок-инструкцию в `analyze-prompt.md`**

В `plugin/skills/review-pr/references/analyze-prompt.md` после пункта про «Report only real problems…» (строка 6, перед `<!-- include: _common/tool-usage.md -->`) вставить:

```markdown
- A unit may include a `structural_summary`: a compact symbol-level overview of
  what changed in this file — changed signatures, added and removed symbols.
  Use it to orient on the contract and to prioritise blast-radius checks BEFORE
  reading the raw diff line by line. The raw `patch` and `commentable_right` /
  `commentable_left` remain the source of truth for exact line numbers; the
  summary never replaces them.
```

- [ ] **Step 2: Запустить guard-тесты промптов**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (include-маркеры и соответствие findings-schema не затронуты).

- [ ] **Step 3: Полный прогон тестов и линт всего пакета**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены по умолчанию).

Run: `.venv/bin/ruff check .`
Expected: без НОВЫХ ошибок в затронутых файлах (репозиторий мог иметь предсуществующие замечания — не гнаться за repo-wide clean; убедиться, что наши файлы чисты).

- [ ] **Step 4: Коммит**

```bash
git add plugin/skills/review-pr/references/analyze-prompt.md
git commit -m "docs(skills): научить analyze-промпт использовать structural_summary (PRI-158)"
```

---

## Self-Review (выполнено при написании плана)

- **Spec coverage:** §1 struct_diff (Tasks 1-3) · §2 ре-экспорт impact (Task 1) · §3 поле ReviewUnit (Task 4) · §4 prepare-вычисление (Task 4) · §5 payload (Task 5) · §6 промпт (Task 6) · §7 тесты (в каждой задаче) — покрыто.
- **Placeholder scan:** код приведён полностью в каждом шаге; плейсхолдеров нет.
- **Type consistency:** `diff_symbols(path, base_source, head_source) -> list[SymbolChange]`, `format_struct_summary(list[SymbolChange]) -> str`, `ReviewUnit.structural_summary: str` — имена/типы согласованы между задачами и со спекой.
- **Известная ловушка (verification):** в тестах `review_service`/`mcp` `chunk_python` замокан на уровне модуля-вызывающего, но `diff_symbols` зовёт РЕАЛЬНЫЙ `chunk_python` из `struct_diff` — поэтому для непустой сводки тесты подают разный реальный Python в base/head; на одинаковых исходниках сводка пуста и старые тесты не ломаются.
