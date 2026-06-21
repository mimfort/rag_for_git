# Унификация dimension-скилов: вынос общего boilerplate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Вынести дублирующийся include-free boilerplate (секция Scope + общий хвост Output) из `performance-review/SKILL.md` и `maintainability-review/SKILL.md` в `plugin/skills/_common/`, оставив в каждом скиле только специфику.

**Architecture:** Вариант A (прагматичный вынос). Два новых include-free файла в `_common/` (`dimension-scope.md`, `dimension-output-tail.md`), подключаемых маркером `<!-- include: _common/X.md -->` на верхнем уровне каждого `SKILL.md`. Вложенные маркеры (`tool-usage`, `findings-schema`) и category-специфика остаются в `SKILL.md` — резолвер нерекурсивный, поэтому новые `_common`-файлы маркеров не содержат.

**Tech Stack:** Markdown-скилы Claude Code-плагина (`plugin/skills/`), pytest guard-тесты (`tests/skills/`), ruff.

## Global Constraints

- Резолв include-маркеров **нерекурсивный**: `_common/*.md` НЕ должны содержать `<!-- include:` (иначе `tests/skills/test_assembled_prompts.py::assemble` упадёт на ассерте «неразрешённый include»).
- Frontmatter `description` обоих скилов **не менять** (это триггеры активации).
- Собранные промпты должны сохранять токены, проверяемые guard-тестами: perf → `"category": "performance"`, `"confidence": 0.0`, `N+1`; maint → `"confidence": 0.0`, `What Not To Flag`.
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- Ветка работы: `refactor/dimension-skills-dedup` (уже создана).
- Команды pytest/ruff — через `.venv/bin/` (см. CLAUDE.md).
- Семантически допустимое единственное изменение текста: строка «If there are no meaningful **performance/maintainability** findings…» → «If there are no meaningful findings…» (слово category убрано, чтобы строка стала общей).

---

### Task 1: Новые `_common`-блоки + расширение guard-тестов

Создаём два общих файла и сразу закрываем их guard-тестами (существование/непустота + запрет вложенных маркеров). Тест пишется первым (RED), затем создаются файлы (GREEN).

**Files:**
- Modify: `tests/skills/test_common_blocks.py:11-19` (переименовать тест, расширить список до 6 файлов; добавить новый тест-guard)
- Create: `plugin/skills/_common/dimension-scope.md`
- Create: `plugin/skills/_common/dimension-output-tail.md`

**Interfaces:**
- Produces: файлы `_common/dimension-scope.md` и `_common/dimension-output-tail.md`, подключаемые Task 2 и Task 3 маркерами `<!-- include: _common/dimension-scope.md -->` и `<!-- include: _common/dimension-output-tail.md -->`.

- [ ] **Step 1: Обновить guard-тест существования и добавить guard на отсутствие маркеров**

В `tests/skills/test_common_blocks.py` заменить функцию `test_all_four_common_files_exist_nonempty` (строки 11–19) на расширенную версию и добавить новый тест сразу после неё:

```python
def test_all_common_files_exist_nonempty():
    for name in (
        "findings-schema.md",
        "anti-hallucination.md",
        "tool-usage.md",
        "branch-selection.md",
        "dimension-scope.md",
        "dimension-output-tail.md",
    ):
        assert (COMMON / name).is_file(), f"нет {name}"
        assert len(_read(name).strip()) > 0, f"{name} пустой"


def test_common_files_have_no_include_markers():
    # Резолвер include нерекурсивный (test_assembled_prompts.assemble подставляет
    # за один проход), поэтому _common-файлы не должны содержать include-маркеров.
    for path in sorted(COMMON.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert "<!-- include:" not in text, f"{path.name} содержит include-маркер"
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_common_blocks.py::test_all_common_files_exist_nonempty -q`
Expected: FAIL с `AssertionError: нет dimension-scope.md` (новые файлы ещё не созданы).

- [ ] **Step 3: Создать `plugin/skills/_common/dimension-scope.md`**

Содержимое (точная копия секции Scope из текущих скилов, без завершающих строк с tool-usage):

```markdown
## Scope

Standalone: ask the user which diff to review if the scope is not clear:

- `staged` — review only the staged diff;
- `unstaged` — review only the unstaged diff;
- uncommitted changes — staged plus unstaged;
- branch-vs-base — compare the current branch against its base branch (state the
  base branch used; infer from upstream, remote default, or common names: `main`,
  `master`, `develop`, `trunk`);
- commit, branch comparison, file list, or PR-like scope — review exactly that.

Do not pick a scope yourself unless the user already made it clear. If the
resulting diff is empty, stop and say there is nothing to review.

Inside `/reviewer_review-pr`: the orchestrator provides the diffs of all units (path + patch)
— review those.
```

- [ ] **Step 4: Создать `plugin/skills/_common/dimension-output-tail.md`**

Содержимое (общий хвост Output; слово category в строке про пустой результат убрано):

```markdown
Standalone runs may additionally render the findings as a readable list after the JSON.

If a finding cannot be tied to a specific line, use the closest changed line and
explain the scope in `message`.

If there are no meaningful findings, return `{"findings": []}` and say so.

Write `message` and `suggestion` in the output language given by the orchestrator
(standalone: the user's language).
```

- [ ] **Step 5: Запустить guard-тесты `_common` — убедиться, что проходят**

Run: `.venv/bin/pytest tests/skills/test_common_blocks.py -q`
Expected: PASS (все тесты, включая `test_all_common_files_exist_nonempty` и `test_common_files_have_no_include_markers`).

- [ ] **Step 6: Убедиться, что остальные skills-тесты не сломаны**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (скилы ещё не тронуты, `test_assembled_prompts.py` зелёный — новые `_common`-файлы пока не используются).

- [ ] **Step 7: Коммит**

```bash
git add plugin/skills/_common/dimension-scope.md plugin/skills/_common/dimension-output-tail.md tests/skills/test_common_blocks.py
git commit -m "feat(skills): общие блоки dimension-scope и dimension-output-tail в _common"
```

---

### Task 2: Перевести `performance-review/SKILL.md` на include

**Files:**
- Modify: `plugin/skills/performance-review/SKILL.md` (заменить блок Scope, строки 8–25; заменить хвост Output, строки 72–80)
- Test: `tests/skills/test_assembled_prompts.py::test_performance_assembled_schema_and_goal`

**Interfaces:**
- Consumes: `_common/dimension-scope.md`, `_common/dimension-output-tail.md` (созданы в Task 1).

- [ ] **Step 1: Заменить секцию Scope на include**

В `plugin/skills/performance-review/SKILL.md` заменить блок:

```markdown
## Scope

Standalone: ask the user which diff to review if the scope is not clear:

- `staged` — review only the staged diff;
- `unstaged` — review only the unstaged diff;
- uncommitted changes — staged plus unstaged;
- branch-vs-base — compare the current branch against its base branch (state the
  base branch used; infer from upstream, remote default, or common names: `main`,
  `master`, `develop`, `trunk`);
- commit, branch comparison, file list, or PR-like scope — review exactly that.

Do not pick a scope yourself unless the user already made it clear. If the
resulting diff is empty, stop and say there is nothing to review.

Inside `/reviewer_review-pr`: the orchestrator provides the diffs of all units (path + patch)
— review those.
```

на одну строку:

```markdown
<!-- include: _common/dimension-scope.md -->
```

(Следующие строки — пустая, `<!-- include: _common/tool-usage.md -->`, `Use the PR-session tools above.` — оставить без изменений.)

- [ ] **Step 2: Заменить хвост Output на include**

В том же файле заменить блок:

```markdown
Standalone runs may additionally render the findings as a readable list after the JSON.

If a finding cannot be tied to a specific line, use the closest changed line and
explain the scope in `message`.

If there are no meaningful performance findings, return `{"findings": []}` and say so.

Write `message` and `suggestion` in the output language given by the orchestrator
(standalone: the user's language).
```

на:

```markdown
<!-- include: _common/dimension-output-tail.md -->
```

(Строки выше — `<!-- include: _common/findings-schema.md -->` и `Set "category" to "performance"; "side" is always "RIGHT".` — оставить.)

- [ ] **Step 3: Запустить guard-тест собранного промпта perf**

Run: `.venv/bin/pytest tests/skills/test_assembled_prompts.py::test_performance_assembled_schema_and_goal -q`
Expected: PASS (в собранном промпте есть `"category": "performance"`, `"confidence": 0.0`, `N+1`; неразрешённых `<!-- include:` нет).

- [ ] **Step 4: Коммит**

```bash
git add plugin/skills/performance-review/SKILL.md
git commit -m "refactor(skills): performance-review через общие _common-блоки"
```

---

### Task 3: Перевести `maintainability-review/SKILL.md` на include + финальная верификация

**Files:**
- Modify: `plugin/skills/maintainability-review/SKILL.md` (заменить блок Scope, строки 8–25; заменить хвост Output, строки 127–136)
- Test: `tests/skills/test_assembled_prompts.py::test_maintainability_assembled_schema_and_whatnot`

**Interfaces:**
- Consumes: `_common/dimension-scope.md`, `_common/dimension-output-tail.md` (созданы в Task 1).

- [ ] **Step 1: Заменить секцию Scope на include**

В `plugin/skills/maintainability-review/SKILL.md` заменить блок:

```markdown
## Scope

Standalone: ask the user which diff to review if the scope is not clear:

- `staged` — review only the staged diff;
- `unstaged` — review only the unstaged diff;
- uncommitted changes — staged plus unstaged;
- branch-vs-base — compare the current branch against its base branch (state the
  base branch used; infer from upstream, remote default, or common names: `main`,
  `master`, `develop`, `trunk`);
- commit, branch comparison, file list, or PR-like scope — review exactly that.

Do not pick a scope yourself unless the user already made it clear. If the
resulting diff is empty, stop and say there is nothing to review.

Inside `/reviewer_review-pr`: the orchestrator provides the diffs of all units (path + patch)
— review those.
```

на одну строку:

```markdown
<!-- include: _common/dimension-scope.md -->
```

(Следующие строки — пустая, `<!-- include: _common/tool-usage.md -->`, `Use the PR-session tools above.` — оставить.)

- [ ] **Step 2: Заменить хвост Output на include**

В том же файле заменить блок:

```markdown
Standalone runs may additionally render the findings as a readable list after the JSON.

If a finding cannot be tied to a specific line, use the closest changed line and
explain the scope in `message`.

If there are no meaningful maintainability findings, return `{"findings": []}` and
say so.

Write `message` and `suggestion` in the output language given by the orchestrator
(standalone: the user's language).
```

на:

```markdown
<!-- include: _common/dimension-output-tail.md -->
```

(Абзац выше — `The `suggestion` field replaces what in the original Codex format appeared after / `Simplification:` — put the concrete simplifying alternative there.` — оставить на месте: он сохраняет свою исходную позицию между category-строкой и общим хвостом.)

- [ ] **Step 3: Запустить guard-тест собранного промпта maint**

Run: `.venv/bin/pytest tests/skills/test_assembled_prompts.py::test_maintainability_assembled_schema_and_whatnot -q`
Expected: PASS (в собранном промпте есть `"confidence": 0.0`, `What Not To Flag`; неразрешённых `<!-- include:` нет).

- [ ] **Step 4: Полная верификация skills-тестов и линта**

Run: `.venv/bin/pytest tests/skills/ -q && .venv/bin/ruff check plugin tests/skills`
Expected: PASS (все skills-тесты зелёные; ruff без ошибок по затронутым путям).

- [ ] **Step 5: Глазами сверить эквивалентность собранных промптов**

Run: `python -c "import re,pathlib; S=pathlib.Path('plugin/skills'); I=re.compile(r'<!-- include: (\S+\.md) -->'); a=lambda p:(I.sub(lambda m:(S/m.group(1)).read_text('utf-8'),(S/p).read_text('utf-8'))); print(a('performance-review/SKILL.md')); print('=====MAINT====='); print(a('maintainability-review/SKILL.md'))"`
Expected: оба собранных промпта читаются как прежде; единственные отличия от старой версии — строка «no meaningful findings» без слова category (ожидаемо). Никаких `<!-- include:` в выводе.

- [ ] **Step 6: Коммит**

```bash
git add plugin/skills/maintainability-review/SKILL.md
git commit -m "refactor(skills): maintainability-review через общие _common-блоки"
```

---

## Self-Review

**1. Spec coverage:**
- Вынос Scope → Task 1 (создание `dimension-scope.md`) + Task 2/3 (подключение). ✓
- Вынос общего хвоста Output → Task 1 (`dimension-output-tail.md`) + Task 2/3. ✓
- Frontmatter не тронут → правки Scope/Output не задевают frontmatter. ✓
- Guard-тест существования расширен + новый guard на маркеры → Task 1. ✓
- `test_assembled_prompts.py` остаётся зелёным → проверяется в Task 2 Step 3, Task 3 Step 3/4. ✓
- Нерекурсивный инвариант кодифицирован → Task 1 `test_common_files_have_no_include_markers`. ✓
- Method не выносится (различается) → план его не трогает. ✓

**2. Placeholder scan:** Плейсхолдеров нет — всё содержимое файлов и команды приведены дословно.

**3. Type consistency:** Имена файлов `_common/dimension-scope.md` и `_common/dimension-output-tail.md`, а также имена маркеров согласованы между Task 1 (создание + guard) и Task 2/3 (подключение). Имена тестов (`test_all_common_files_exist_nonempty`, `test_common_files_have_no_include_markers`) согласованы между Step-командами.
