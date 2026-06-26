# PRI-162 solve-task: тест-образцы (include_tests) для TDD-хендоффа — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в скилл `solve-task` опциональный тест-ретрив (`include_tests=True`) и секцию «Test exemplars» в бриф, чтобы TDD-хендофф стартовал с конкретного тест-паттерна для mimic.

**Architecture:** Чисто промптовая правка одного скилл-файла `plugin/skills/solve-task/SKILL.md` (шаги 3–4), застрахованная guard-тестом в `tests/skills/test_solve_task_brief.py`. Движок не трогаем — флаг `include_tests=True` уже проброшен end-to-end (`reviewer/mcp/service.py:405` → `reviewer/retrieval/retriever.py:138`).

**Tech Stack:** Markdown (SKILL.md), Python (pytest guard-тест), `.venv/bin/pytest`.

## Global Constraints

- Скоуп — **только** `plugin/skills/solve-task/SKILL.md` + `tests/skills/test_solve_task_brief.py`. Никаких правок Python-движка/ретривера, никаких новых MCP-параметров.
- Тело скилла — по-английски (экономия токенов); пользовательские ответы скилла — по-русски (соглашение проекта). Заголовок секции брифа — английский `## Test exemplars` (консистентно со скелетом).
- Guard-тесты в `tests/skills/` пинят **стабильные маркеры**, не дословные формулировки.
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких Co-Authored-By / упоминаний Claude).
- Линт: `.venv/bin/ruff check .` (line-length 100, py311).
- Работа идёт на ветке `feat/pri-162-solve-task-include-tests` (спека+бриф уже закоммичены там).

---

### Task 1: Тест-ретрив «Test exemplars» в solve-task + guard-тест

**Files:**
- Test: `tests/skills/test_solve_task_brief.py` (добавить функцию в конец файла)
- Modify: `plugin/skills/solve-task/SKILL.md` (шаг 3: новый под-шаг после буллета код-ретрива; шаг 4: строка в скелете брифа + правки фильтра релевантности)

**Interfaces:**
- Consumes: существующий MCP-тул `search_codebase(repo, query, top_k, branch, include_tests=False)` — параметр `include_tests` уже есть (`reviewer/mcp/service.py:396-417`). Менять его не нужно.
- Produces: guard-тест `test_solve_task_includes_test_exemplars` + два стабильных маркера в `SKILL.md` (`include_tests=True`, `Test exemplars`), на которые опирается тест.

- [ ] **Step 1: Написать падающий guard-тест**

В конец `tests/skills/test_solve_task_brief.py` (после `test_solve_task_resolves_subtask_criteria_when_thin`, текущая последняя строка 52) добавить:

```python


def test_solve_task_includes_test_exemplars():
    """PRI-162: solve-task подмешивает тест-образцы (include_tests) для TDD-хендоффа."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "include_tests=True" in text     # тест-ретрив в шаге 3
    assert "Test exemplars" in text         # секция скелета брифа
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py::test_solve_task_includes_test_exemplars -q`
Expected: FAIL — `AssertionError` на `assert "include_tests=True" in text` (маркеров пока нет в SKILL.md).

- [ ] **Step 3: Шаг 3 — добавить под-шаг «Test exemplars»**

В `plugin/skills/solve-task/SKILL.md`, в шаге 3, **после** буллета код-ретрива (заканчивается на `mimic).` — текущие строки 122-123) и **перед** буллетом `- **Deepen via the code graph` (строка 124), вставить новый буллет. Заменить точно:

Найти:
```
   - `search_codebase("<task description>")` → relevant existing code (files/symbols to touch or
     mimic).
   - **Deepen via the code graph (optional — when `search_codebase` surfaced concrete symbols).**
```

Заменить на:
```
   - `search_codebase("<task description>")` → relevant existing code (files/symbols to touch or
     mimic).
   - **Test exemplars (optional — when `search_codebase` surfaced concrete symbols).** One extra
     `search_codebase("<how the task's area is tested — fixtures/mocks for the feature>", include_tests=True)`
     on the same `branch` — a targeted *test* query (how the area is tested), not the code query with
     the flag flipped, so it surfaces the testing pattern the TDD hand-off should mimic. Snippets are
     line-numbered like the code retrieval → cite `path:line` directly. Apply the same Step 4 rank-based
     filter (≤3 test files/symbols). Fail-open: no tests surfaced / a `(ничего не найдено)` note / an
     error → omit the `## Test exemplars` brief section; the default code retrieval
     (`include_tests=False`) is unchanged.
   - **Deepen via the code graph (optional — when `search_codebase` surfaced concrete symbols).**
```

- [ ] **Step 4: Шаг 4 — добавить секцию в скелет брифа**

В том же файле, в блоке «Brief skeleton», **после** строки `## Relevant code — ...` и **перед** строкой `## Constraints / open questions — ...`, вставить строку секции. Заменить точно:

Найти:
```
   ## Relevant code — ≤5 files/symbols, one line: «path:line — why» (+ blast radius from the graph). (dropped N: …)
   ## Constraints / open questions — terse bullets: limits, unknowns, context gaps (e.g. "board unavailable", "task corpus empty").
```

Заменить на:
```
   ## Relevant code — ≤5 files/symbols, one line: «path:line — why» (+ blast radius from the graph). (dropped N: …)
   ## Test exemplars — ≤3 test files/symbols, one line: «path:line — what's mocked / which pattern». (omit if none; dropped N: …)
   ## Constraints / open questions — terse bullets: limits, unknowns, context gaps (e.g. "board unavailable", "task corpus empty").
```

- [ ] **Step 5: Шаг 4 — обновить колпак фильтра релевантности**

В блоке «Relevance filter», в пункте **Caps**. Заменить точно:

Найти:
```
   - **Caps (ceilings — take fewer if that's enough):** ≤3 related tasks · ≤5 files/symbols in
     Relevant code. Expand the graph (`related_symbols`/`callers`/`definition`) only for the few
     symbols central to the task.
```

Заменить на:
```
   - **Caps (ceilings — take fewer if that's enough):** ≤3 related tasks · ≤5 files/symbols in
     Relevant code · ≤3 test files/symbols in Test exemplars. Expand the graph
     (`related_symbols`/`callers`/`definition`) only for the few symbols central to the task.
```

- [ ] **Step 6: Шаг 4 — обновить пункт «Report what you dropped»**

В том же блоке. Заменить точно:

Найти:
```
   - **Report what you dropped:** end the Related work and Relevant code sections with
     `(dropped N: reason)`.
```

Заменить на:
```
   - **Report what you dropped:** end the Related work, Relevant code and Test exemplars sections with
     `(dropped N: reason)`.
```

- [ ] **Step 7: Запустить guard-тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py::test_solve_task_includes_test_exemplars -q`
Expected: PASS.

- [ ] **Step 8: Прогнать весь файл guard-тестов скилла — не сломали соседей**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py -q`
Expected: PASS — все 6 тестов (5 прежних + новый) зелёные.

- [ ] **Step 9: Прогнать guard-тесты всех скиллов + линт**

Run: `.venv/bin/pytest tests/skills/ -q && .venv/bin/ruff check tests/skills/test_solve_task_brief.py`
Expected: PASS; ruff — без новых замечаний по изменённому тест-файлу.

- [ ] **Step 10: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_solve_task_brief.py
git commit -m "feat(solve-task): тест-образцы через include_tests для TDD-хендоффа (PRI-162)"
```

---

## Self-Review

**1. Spec coverage:**
- §1 Шаг 3 под-шаг «Test exemplars» → Step 3 ✅
- §2 Шаг 4 секция скелета → Step 4 ✅
- §3 Caps + Report dropped → Steps 5–6 ✅
- §4 Guard-тест → Steps 1, 7, 8 ✅
- Критерий «обычный код-ретрив не меняется» → формулировка под-шага сохраняет `include_tests=False` дефолт; ни одна правка не трогает буллет код-ретрива по существу ✅
- Критерий «fail-open при отсутствии тестов» → явная фраза «omit the `## Test exemplars` brief section» ✅

**2. Placeholder scan:** Каждый шаг несёт точный find/replace-текст и точные команды с ожидаемым выводом. `<...>` в примере запроса — намеренный шаблон промпта (так же оформлены соседние под-шаги: `"<task description>"`, `query="<task title>. ..."`). Не пробел плана. ✅

**3. Type consistency:** Маркеры guard-теста (`include_tests=True`, `Test exemplars`) дословно присутствуют во вставляемом тексте Step 3 и Step 4. Заголовок секции единообразен: `## Test exemplars` в скелете (Step 4) и в Caps/Report (Steps 5–6) и в тексте под-шага (Step 3). ✅
