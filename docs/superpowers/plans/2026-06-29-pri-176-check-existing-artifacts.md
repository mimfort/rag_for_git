# PRI-176 check existing artifacts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** В скиле `solve-task` починить glob-паттерн идемпотентности брифа и добавить предупреждение о существующих brief/spec/plan перед перезаписью.

**Architecture:** Изменения исключительно в `plugin/skills/solve-task/SKILL.md` (параграф **Persist the brief**) + guard-тест в `tests/skills/test_solve_task_brief.py`. TDD: сначала падающий guard-тест, потом правка SKILL.md.

**Tech Stack:** Markdown (SKILL.md), Python 3.11+, pytest.

---

## Global Constraints

- Тело `SKILL.md` пишется **по-английски** (соответствие существующему скилу); тест-докстринги — на русском.
- Без правок движка/БД/миграций.
- Коммиты: Conventional Commits на русском, **без self-attribution**.
- Линт: `ruff check tests/skills/test_solve_task_brief.py`.
- Guard-тесты — маркер-проверки текста SKILL.md, не пинят точные формулировки.

---

### Task 1: Guard-тест для новой логики

**Files:**
- Modify: `tests/skills/test_solve_task_brief.py`

**Interfaces:**
- Consumes: текущий `plugin/skills/solve-task/SKILL.md`.
- Produces: падающий guard-тест `test_solve_task_warns_on_existing_artifacts`.

- [ ] **Step 1: Добавить падающий guard-тест**

В конец `tests/skills/test_solve_task_brief.py` добавить:

```python

def test_solve_task_warns_on_existing_artifacts():
    """PRI-176: solve-task проверяет существующие briefs/specs/plans и предупреждает, не блокируя."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "*-<KEY>-*.md" in text               # glob без даты
    assert "docs/superpowers/specs/" in text    # проверка спек
    assert "docs/superpowers/plans/" in text    # проверка планов
    assert "case-insensitive" in text            # insensitive matching
    assert "[Y/n]" in text                       # предупреждение с выбором
    assert "[existing_artifacts]" in text        # тег в Constraints
    assert "Do NOT block" in text or "not block" in text  # не блокировка
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py::test_solve_task_warns_on_existing_artifacts -q`

Expected: FAIL — первый ассерт `assert "*-<KEY>-*.md" in text` не проходит, т.к. в SKILL.md ещё старый паттерн `<date>-<KEY>-*.md`.

---

### Task 2: Правка SKILL.md

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (параграф **Persist the brief**, строки ~199–212)

**Interfaces:**
- Consumes: текущий параграф **Persist the brief**.
- Produces: исправленный glob + новый подпункт **Check for existing artifacts**.

- [ ] **Step 3: Починить glob-паттерн**

В `plugin/skills/solve-task/SKILL.md` заменить:

```markdown
   - **Idempotency:** before writing, glob `docs/superpowers/briefs/<date>-<KEY>-*.md` and overwrite
     the match if any (slug drift between runs must not spawn duplicates); board-less → exact name.
```

на:

```markdown
   - **Idempotency:** before writing, glob `docs/superpowers/briefs/*-<KEY>-*.md` and overwrite
     the match if any (slug drift between runs must not spawn duplicates); board-less → exact name.
```

- [ ] **Step 4: Добавить подпункт проверки существующих артефактов**

В `plugin/skills/solve-task/SKILL.md`, **перед** подпунктом **Idempotency** (т.е. между **Content** и **Idempotency**, или в начале списка после введения параграфа), вставить:

```markdown
   - **Check for existing artifacts (warn, don't block).** Before writing the brief, scan the
     three artifact directories for files matching this task key (case-insensitive):
     - `docs/superpowers/briefs/*<KEY>*`
     - `docs/superpowers/specs/*<key>*-design.md`
     - `docs/superpowers/plans/*<key>*.md`
     Use case-insensitive matching (e.g., try both `PRI-176` and `pri-176` globs, or lowercase
     file names before matching). If any artifacts are found, warn the user (in Russian):
     > "⚠️ Похожие артефакты уже существуют: briefs/PRI-176-..., specs/pri-176-...-design.md,
     > plans/pri-176-....md. Продолжить? [Y/n]"
     Do **not** block — continue unless the user explicitly says no. If the user continues (or
     auto-permission mode leaves no choice), list the found artifacts under `## Constraints` with
     the tag `[existing_artifacts]`.
```

**Placement note:** этот подпункт должен находиться внутри параграфа **Persist the brief**, желательно перед **Idempotency**, чтобы логика была: проверить → предупредить → перезаписать.

- [ ] **Step 5: Запустить guard-тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py::test_solve_task_warns_on_existing_artifacts -q`

Expected: PASS.

---

### Task 3: Регрессионный прогон и линт

**Files:**
- Test: `tests/skills/test_solve_task_brief.py`
- Test: `tests/skills/` (весь каталог)

- [ ] **Step 6: Прогнать весь `tests/skills/` — нет регрессий**

Run: `.venv/bin/pytest tests/skills/ -q`

Expected: PASS (все guard-тесты скиллов зелёные).

- [ ] **Step 7: Линт**

Run: `.venv/bin/ruff check tests/skills/test_solve_task_brief.py`

Expected: без ошибок в изменённом файле.

---

### Task 4: Коммит

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md`
- Modify: `tests/skills/test_solve_task_brief.py`

- [ ] **Step 8: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_solve_task_brief.py
git commit -m "feat(solve-task): проверка существующих briefs/specs/plans + починка glob (PRI-176)"
```

---

## Self-Review

**1. Spec coverage:**
- Исправление glob на `*-<KEY>-*.md` → Task 2 Step 3. ✓
- Проверка `briefs/`/`specs/`/`plans/` → Task 2 Step 4. ✓
- Case-insensitive matching → Task 2 Step 4. ✓
- Предупреждение `[Y/n]` и не-блокировка → Task 2 Step 4. ✓
- Тег `[existing_artifacts]` в Constraints → Task 2 Step 4. ✓
- Guard-тест → Task 1. ✓
- Регрессионный прогон + линт → Task 3. ✓

**2. Placeholder scan:**
- Нет TBD/TODO.
- Все шаги содержат конкретные команды и код.
- Пути файлов точные.

**3. Type/marker consistency:**
- Маркеры теста (`*-<KEY>-*.md`, `docs/superpowers/specs/`, `docs/superpowers/plans/`, `case-insensitive`, `[Y/n]`, `[existing_artifacts]`, `not block`) совпадают с текстом, добавляемым в SKILL.md Task 2 Step 4.
