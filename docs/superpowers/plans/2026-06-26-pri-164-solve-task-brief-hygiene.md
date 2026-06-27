# PRI-164 solve-task brief hygiene — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** В скиле `solve-task` дорезолвить `criteria` из подзадач при тонком `description` и явно дедупить related-источники (linked ∪ similar) — обе правки только в `SKILL.md` + guard-тесты.

**Architecture:** Изменения исключительно в промпт-доке `plugin/skills/solve-task/SKILL.md` (шаги 2–4). TDD здесь = guard-тесты в `tests/skills/test_solve_task_brief.py`, которые grep'ают стабильные маркеры инструкций (репо-конвенция `tests/skills/`). Сначала падающий ассерт на отсутствующий маркер → правка `SKILL.md` → зелёный.

**Tech Stack:** Python 3.11+, pytest. Тело `SKILL.md` — на английском (токены), как и весь скил; тест-докстринги/комментарии — на русском (как соседние тесты).

## Global Constraints

- Тело `SKILL.md` пишется **по-английски** (соответствие существующему скилу); ответы пользователю скил инструктирует по-русски — этого не меняем.
- Без правок движка/БД/миграций — серверный путь criteria-колонки **вне скоупа** (спека).
- Часть (a) — **fail-open** и **не вызывает `index_task`**; обогащённые criteria идут только в бриф.
- Сохранить YAGNI инлайн-критериев: при наличии заголовка критериев в `description` поведение не меняется (`criteria=[]`).
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких Co-Authored-By/упоминаний Claude).
- Ветка работы: `feat/pri-164-solve-task-brief-hygiene` (уже создана; спека+бриф закоммичены).
- Прогон тестов: `.venv/bin/pytest tests/skills/test_solve_task_brief.py -q`. Линт: `.venv/bin/ruff check tests/skills/`.

---

### Task 1: (b) Дедуп related-источников по ключу

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (шаг 3 — после bullet'а `search_tasks`, ~строка 109; шаг 4 — relevance-фильтр, после bullet'а «Report what you dropped», ~строка 157)
- Test: `tests/skills/test_solve_task_brief.py`

**Interfaces:**
- Consumes: текущий `SKILL.md` шаги 3–4 (см. якоря ниже).
- Produces: стабильные маркеры `Dedup related sources by key` и `linked ∪ similar` в `SKILL.md`.

- [ ] **Step 1: Написать падающий тест**

В `tests/skills/test_solve_task_brief.py` добавить в конец файла:

```python
def test_solve_task_dedupes_related_sources():
    """PRI-164(b): «Related work» дедупится по ключу между linked и similar."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "Dedup related sources by key" in text   # явный шаг дедупа
    assert "linked ∪ similar" in text               # оба источника, слитые
    assert "canonical task key" in text             # дедуп по каноническому ключу
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py::test_solve_task_dedupes_related_sources -q`
Expected: FAIL — `assert "Dedup related sources by key" in text` (маркера ещё нет).

- [ ] **Step 3: Добавить инструкцию дедупа в `SKILL.md`**

(3a) В **шаге 3**, сразу ПОСЛЕ bullet'а `search_tasks` (строки 108–109, заканчивается на «…for fuller detail.»), вставить новый bullet:

```markdown
   - **Related work = linked ∪ similar.** The «Related work» brief section draws from two sources —
     `get_task_context` (linked) and `search_tasks` (similar). They overlap; the Step 4 filter
     deduplicates them by key before the cap.
```

(3b) В **шаге 4**, в списке relevance-фильтра, сразу ПОСЛЕ bullet'а «**Report what you dropped:** …» (строки 156–157), вставить новый bullet:

```markdown
   - **Dedup related sources by key (linked ∪ similar).** «Related work» draws from
     `get_task_context` (linked) and `search_tasks` (similar). Deduplicate by canonical task key
     BEFORE the ≤3 cap, matching `PRI-N`↔`ID-N` via `aliases` (one task, two codes). On collision
     keep the linked entry (richer — carries PR/graph context) and drop the similar duplicate, so a
     task never appears twice in the brief.
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py::test_solve_task_dedupes_related_sources -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_solve_task_brief.py
git commit -m "feat(solve-task): дедуп related-источников linked ∪ similar по ключу (PRI-164)"
```

---

### Task 2: (a) Резолв subtask-criteria при тонком description

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (шаг 2, ветка **Hit**, после строки 81 «…came from the reviewer store (after sync).»)
- Test: `tests/skills/test_solve_task_brief.py`

**Interfaces:**
- Consumes: текущий `SKILL.md` шаг 2 ветка Hit (см. якорь ниже).
- Produces: стабильные маркеры `Thin-criteria enrichment` и детектор `(?i)(критери|приёмк|acceptance)` в `SKILL.md`.

- [ ] **Step 1: Написать падающий тест**

В `tests/skills/test_solve_task_brief.py` добавить в конец файла:

```python
def test_solve_task_resolves_subtask_criteria_when_thin():
    """PRI-164(a): при тонком description критерии дорезолвятся из подзадач (fail-open, без index_task)."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "Thin-criteria enrichment" in text                 # шаг присутствует
    assert "(?i)(критери|приёмк|acceptance)" in text          # детектор «тонкого» description
    assert "subtasks" in text                                  # источник критериев — подзадачи
    assert "do NOT call `index_task`" in text                  # обогащение только в бриф
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py::test_solve_task_resolves_subtask_criteria_when_thin -q`
Expected: FAIL — `assert "Thin-criteria enrichment" in text` (маркера ещё нет).

- [ ] **Step 3: Добавить инструкцию обогащения в `SKILL.md` (шаг 2, ветка Hit)**

В **шаге 2**, в ветке **Hit**, сразу ПОСЛЕ строки «…Note in the brief that the task data came from the reviewer store (after sync).» (строка 81), вставить вложенный bullet (отступ 10 пробелов — уровень вложенности под Hit):

```markdown
          - **Thin-criteria enrichment (optional, fail-open).** The store returns `criteria=[]` —
            requirements normally live in `description`. If `description` has NO acceptance-criteria
            heading (no section matching `(?i)(критери|приёмк|acceptance)`) AND a board is connected,
            resolve the task's subtasks into `criteria[]` via the board-MCP playbook
            `../review-pr/references/task-context-<task_board.type>.md` (its «Criteria note»):
            one board `get_task(key)` → for each `subtasks[]` id resolve its title. Fold the resolved
            criteria into the brief's `## Task` section only — do NOT call `index_task`. When the
            heading IS present, criteria are inline in `description` → skip (leave `[]`). No board /
            no subtasks / any error → leave `criteria` empty.
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py::test_solve_task_resolves_subtask_criteria_when_thin -q`
Expected: PASS.

- [ ] **Step 5: Регрессионный прогон всей skills-сьюты + линт**

Run: `.venv/bin/pytest tests/skills/ -q && .venv/bin/ruff check tests/skills/`
Expected: все тесты PASS (включая существующие `test_solve_task_*` и `assemble`-guard'ы); ruff — чисто на затронутом файле.

> Если упадёт `test_assembled_prompts` или другой include/guard-тест — значит правка задела include-маркеры или обязательные шаги; вернуться к Step 3 и поправить, не удаляя существующих маркеров (`# Brief —`, `≤3`, `≤5`, `(dropped`, `directly informs`, `project=`, `docs/superpowers/briefs/`).

- [ ] **Step 6: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_solve_task_brief.py
git commit -m "feat(solve-task): резолв subtask-criteria при тонком description (PRI-164)"
```

---

## Self-Review

**Spec coverage:**
- (a) резолв subtask-criteria при тонком description → Task 2. ✓ (детектор по заголовку, board-MCP-плейбук, только в бриф, fail-open, без `index_task`).
- (b) дедуп related-источников linked ∪ similar по ключу → Task 1. ✓ (дедуп до капа ≤3, `PRI-N↔ID-N` через aliases, linked в приоритете).
- Тесты в `tests/skills/test_solve_task_brief.py` → Task 1 Step 1 + Task 2 Step 1. ✓
- Серверный путь вне скоупа → не планируется. ✓
- Fail-open / без `index_task` / YAGNI инлайн-критериев → зашиты в текст инструкции Task 2 Step 3 и в тест-ассерты. ✓

**Placeholder scan:** план содержит полный текст всех правок и тестов; плейсхолдеров нет. ✓

**Type/marker consistency:** маркеры, на которые ассертит тест, совпадают с текстом, вставляемым в `SKILL.md`:
- Task 1: `Dedup related sources by key`, `linked ∪ similar`, `canonical task key` — все присутствуют в тексте Step 3b. ✓
- Task 2: `Thin-criteria enrichment`, `(?i)(критери|приёмк|acceptance)`, `subtasks`, `do NOT call \`index_task\`` — все присутствуют в тексте Step 3. ✓

**Порядок задач:** Task 1 (b, проще, шаги 3–4) → Task 2 (a, шаг 2 + регрессионный прогон). Каждая задача независимо тестируема и коммитится отдельно.
