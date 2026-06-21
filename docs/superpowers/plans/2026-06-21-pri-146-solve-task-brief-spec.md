# solve-task brief spec + relevance-фильтр — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрепить в `plugin/skills/solve-task/SKILL.md` (шаги 3–4) спеку компактного solution brief
и жёсткий ранговый relevance-фильтр, защитив результат тонким guard-тестом.

**Architecture:** Чистая markdown-guidance правка — переписываем шаг 4 (правила фильтра + скелет-шаблон
+ бюджеты) и добавляем связующую строку в шаг 3. Новый guard-тест в `tests/skills/` ассертит маркеры
спеки, чтобы будущая правка не удалила её молча. Поведенческого кода нет.

**Tech Stack:** Markdown (skill body), Python/pytest (guard-тест).

## Global Constraints

- Тело SKILL.md — **на английском** (конвенция репозитория); user-facing brief агент пишет
  по-русски. Скелет и хвост dropped-count пишем по-английски `(dropped N: …)`; в Russian-брифе агент
  локализует как «(отсеяно N: …)».
- **Не трогать** шаг 0 (Preflight) и include-маркеры `<!-- include: _common/tool-usage.md -->`
  (`SKILL.md:97`) и `<!-- include: _common/branch-selection.md -->` (`SKILL.md:102`) — их охраняют
  `tests/skills/test_preflight_guardrail.py` и `test_assembled_prompts.py`.
- Колпаки — **потолки, не квоты:** ≤3 related tasks, ≤5 файлов/символов.
- Relevance — **ранговый**, без абсолютного порога по score (RRF, k=60, `1.0/(60+rank)`,
  `reviewer/tasks/store.py:182-189`).
- Conventional Commits на русском, **без self-attribution**.

---

### Task 1: Guard-тест + спека brief в SKILL.md

**Files:**
- Create: `tests/skills/test_solve_task_brief.py`
- Modify: `plugin/skills/solve-task/SKILL.md` — шаг 3 (вставка перед `:97`) и шаг 4 (замена `:104-115`)

**Interfaces:**
- Consumes: ничего (первая и единственная задача).
- Produces: маркеры в `solve-task/SKILL.md` — `# Brief —`, `≤3`, `≤5`, `(dropped`, `directly informs`
  — на которые опирается guard-тест.

- [ ] **Step 1: Написать падающий guard-тест**

Создать `tests/skills/test_solve_task_brief.py`:

```python
"""Guardrail: solve-task фиксирует спеку brief + ранговый relevance-фильтр (PRI-146).

Шаг 4 SKILL.md должен нести: скелет-шаблон brief, колпаки top-3/top-5,
dropped-count и бинарное правило релевантности. Тест не пинит точные
формулировки — только стабильные маркеры спеки, чтобы правка не удалила её молча.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOLVE = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"


def test_solve_task_brief_spec_present():
    text = SOLVE.read_text(encoding="utf-8")
    assert "# Brief —" in text         # скелет-шаблон brief
    assert "≤3" in text                 # колпак related tasks
    assert "≤5" in text                 # колпак relevant code
    assert "(dropped" in text           # конвенция dropped-count
    assert "directly informs" in text   # бинарное правило релевантности
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py -q`
Expected: FAIL — `assert "# Brief —" in text` (а также `≤3`, `≤5`, `(dropped`) не находятся в текущем
SKILL.md.

- [ ] **Step 3: Заменить шаг 4 в `plugin/skills/solve-task/SKILL.md`**

Заменить блок шага 4 (текущие строки 104–115, от `4. **Distill the solution brief.**` до конца
строки 115 `…do NOT plan or implement here.` — то есть весь пункт 4, **не** трогая пункт 5 «Hand off
to development», который остаётся как есть) на:

````markdown
4. **Distill the solution brief.** Write a structured markdown brief whose only job is to seed
   `brainstorming` — compact, scannable, nothing the implementer won't act on.

   **Relevance filter (rank-based, no absolute score cutoff).** `search_tasks` returns a per-result
   `score`, but it is an RRF rank score (`SUM(1/(60+rank))`, ≈0.016–0.033) — NOT comparable across
   queries, so never gate on an absolute value. `search_codebase` exposes no score at all, only
   result order. Therefore:
   - **Order** candidates by result rank (tasks: rank/score; code: rank).
   - **Caps (ceilings — take fewer if that's enough):** ≤3 related tasks · ≤5 files/symbols in
     Relevant code. Expand the graph (`related_symbols`/`callers`/`definition`) only for the few
     symbols central to the task.
   - **Keep/drop is a binary judgment** — include an item ONLY if it *directly informs the
     implementation*. Rank/score only sets review order and breaks ties at the cap; it is not a
     numeric gate.
     - ✅ INCLUDE: a symbol/file you will edit or mimic; a task whose PR shows a concrete pattern to
       follow; a constraint that narrows the approach.
     - ❌ EXCLUDE: a task in the same area but a different mechanism; a file the search surfaced that
       you won't touch or copy; background you won't act on.
   - **Report what you dropped:** end the Related work and Relevant code sections with
     `(dropped N: reason)`.

   **Brief skeleton — fill it, keep each item to one line:**

   ```
   # Brief — <KEY> <title>
   ## Task — key/title/requirements/criteria (or the user's formulation in board-less mode). ≤~6 lines.
   ## Related work — ≤3 tasks, one line each: «KEY — what to reuse / follow». (dropped N: …)
   ## Relevant code — ≤5 files/symbols, one line: «path:line — why» (+ blast radius from the graph). (dropped N: …)
   ## Constraints / open questions — terse bullets: limits, unknowns, context gaps (e.g. "board unavailable", "task corpus empty").
   ```

   Cite `path:line` straight from the line-numbered Step 3 snippets — no re-Read (Step 3 contract).
````

- [ ] **Step 4: Добавить связующую строку в шаг 3**

В `plugin/skills/solve-task/SKILL.md`, **сразу после** блока «Lazy PR diff (optional)» (после текущей
строки 95, заканчивающейся `fetch diffs for low-relevance tasks).`) и **перед** пустой строкой/маркером
`<!-- include: _common/tool-usage.md -->` (строка 97), вставить новый буллет:

```markdown
   - **Relevance signals → Step 4 filter.** `search_tasks` `score` is an RRF rank score
     (≈0.016–0.033), not comparable across queries; `search_codebase` has no score, only order.
     Carry *rank/order* — not absolute score — into the Step 4 filter, and fetch `get_pr_diff`
     only for a related task that survives that filter (within top-3, directly informing).
```

- [ ] **Step 5: Прогнать guard-тест — убедиться, что зелёный**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py -q`
Expected: PASS.

- [ ] **Step 6: Прогнать все skill-guard тесты — нет регресса**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (включая `test_preflight_guardrail.py` и `test_assembled_prompts.py` — include-маркеры
и Preflight целы).

- [ ] **Step 7: Линт**

Run: `.venv/bin/ruff check tests/skills/test_solve_task_brief.py`
Expected: без ошибок.

- [ ] **Step 8: Коммит**

```bash
git add tests/skills/test_solve_task_brief.py plugin/skills/solve-task/SKILL.md
git commit -m "feat(skills): спека brief + ранговый relevance-фильтр в solve-task (PRI-146)"
```

---

## Self-Review

**1. Spec coverage:**
- Спека шага 4 (правила фильтра + скелет + бюджеты) → Steps 3. ✅
- Ранговый фильтр без абсолютного порога → Step 3 (правило `Order by rank`, no numeric gate). ✅
- Колпаки ≤3/≤5 → Step 3 (Caps). ✅
- Примеры include/exclude → Step 3 (✅/❌). ✅
- Dropped-count → Step 3 (`(dropped N: reason)`). ✅
- Гейтинг ленивого `get_pr_diff` фильтром → Step 4 (связующий буллет шага 3). ✅
- Связующие строки в шаге 3 → Step 4. ✅
- Guard-тест → Steps 1–2, 5. ✅
- Include-маркеры/Preflight целы → Step 6. ✅

**2. Placeholder scan:** плейсхолдеров плана нет; `<KEY>`/`<title>`/`N`/`…` — намеренные слоты
скелета внутри SKILL.md, не пробелы плана. ✅

**3. Type consistency:** не применимо (нет кода/сигнатур). Маркеры guard-теста (`# Brief —`, `≤3`,
`≤5`, `(dropped`, `directly informs`) дословно совпадают со строками, добавляемыми в Steps 3–4. ✅
