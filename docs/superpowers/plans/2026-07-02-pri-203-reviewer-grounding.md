# PRI-203 — Reviewer-грунтовка за пределами брифа (план/ревью) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать использовать session-less reviewer-тулы за пределами брифа (план/ревью) — точечно, fail-open: чиним владеемые ревью-скилы кодом, вендоренные фазы включаем opt-in блоком в README.

**Architecture:** Единый reference-блок `_common/reviewer-grounding.md` подключается в standalone-ревью-скилы плагина (`maintainability-review`, `performance-review`), делая их session-less-грунтованными с fail-open в grep. Для вендоренных фаз (`writing-plans`/`brainstorming`) — копипаст-блок в `README.md` (EN) и `README.ru.md` (RU), который пользователь вставляет в свой контекст-файл; догфуд — в `CLAUDE.md` этого репо. Движок не трогаем (session-less impact НЕ делаем; `callers` достаточно).

**Tech Stack:** Markdown-скилы плагина (`plugin/skills/`), pytest guard-тесты (`tests/skills/`), ruff. Без изменений Python-движка.

## Global Constraints

- **Ветка:** работать на `feat/pri-203-reviewer-grounding` (off `dev`), НЕ коммитить в `dev` напрямую. Реализация через subagent-driven-development в git-worktree.
- **Модель субагентов:** Opus (директива пользователя, переопределяет дефолт «код → Sonnet»).
- **Коммиты:** Conventional Commits на русском, БЕЗ self-attribution (никаких `Co-Authored-By`/Claude).
- **Язык:** новые `_common`/скилл-тексты — английские (как прочие `_common/*.md`); `README.ru.md`/`CLAUDE.md`-блок и сообщения — русские.
- **Инвариант include:** `_common/*.md` не содержат маркеров `<!-- include: -->` (нерекурсивный резолвер; guard-тест `test_common_files_have_no_include_markers`).
- **Fail-open / standalone-baseline:** грунтовка строго опциональна; дефолт при отсутствии/устаревании reviewer — grep/Read; поведение `review-pr`/`ask`/`solve-task` не меняем.
- **Движок:** session-less impact-тул НЕ добавляем; `get_impact` остаётся PR-session-only.

---

### Task 1: Общий блок `_common/reviewer-grounding.md`

**Files:**
- Create: `plugin/skills/_common/reviewer-grounding.md`
- Test: `tests/skills/test_common_blocks.py:11-21` (расширить), + новый тест в этом же файле

**Interfaces:**
- Produces: reference-блок, подключаемый маркером `<!-- include: _common/reviewer-grounding.md -->` (путь от `plugin/skills/`). Без вложенных include-маркеров.

- [ ] **Step 1: Write the failing tests**

В `tests/skills/test_common_blocks.py` добавить `"reviewer-grounding.md"` в кортеж `test_all_common_files_exist_nonempty` (между `"tool-usage.md",` и `"branch-selection.md",`):

```python
    for name in (
        "findings-schema.md",
        "anti-hallucination.md",
        "tool-usage.md",
        "reviewer-grounding.md",
        "branch-selection.md",
        "dimension-scope.md",
        "dimension-output-tail.md",
    ):
```

И добавить новый тест в конец файла:

```python
def test_reviewer_grounding_has_core_rules():
    text = _read("reviewer-grounding.md")
    assert "Reviewer grounding (optional, fail-open)" in text  # заголовок блока
    assert "reviewer status" in text and "drift == 0" in text  # freshness-check
    # session-less тулы перечислены
    assert "search_codebase" in text and "callers" in text \
        and "related_symbols" in text and "definition" in text
    assert "grep" in text.lower()                              # fail-open в grep
    assert "3 RPM" in text                                     # политика «точечно» (Voyage rate-limit)
    assert "base:<branch>" in text                             # честность WIP vs base
    assert "<!-- include:" not in text                         # без вложенных include
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/skills/test_common_blocks.py -q`
Expected: FAIL — `test_all_common_files_exist_nonempty` и `test_reviewer_grounding_has_core_rules` падают (файл `reviewer-grounding.md` отсутствует).

- [ ] **Step 3: Create the block file**

Создать `plugin/skills/_common/reviewer-grounding.md` с содержимым (verbatim):

```
Reviewer grounding (optional, fail-open):

When the reviewer MCP server is connected AND its base index is fresh, prefer the
session-less reviewer tools over raw grep/Read to ground cross-file facts — but only
where it pays. When reviewer is absent or the index is stale, silently fall back to
grep/Read; the standalone baseline is unchanged.

- Freshness check (once): `reviewer status <repo-path> --branch <branch> --json`.
  `drift == 0` -> fresh, use the tools; `drift > 0` -> stale, note it and keep going on
  the stale index (do NOT reindex mid-task); `drift == null` or the command fails ->
  no index, fall back to grep/Read.
- Tools: `search_codebase(repo, query, branch?)` — find relevant code by description;
  `callers(repo, node_id, branch?)` — blast-radius: who calls a symbol whose signature
  you are about to change; `related_symbols(repo, node_id, branch?)` — graph neighbours;
  `definition(repo, symbol, branch?)` — where a symbol is defined. `node_id` is `path#fqn`;
  `search_codebase` snippets are headed by it, so feed that id to the graph tools.
- Targeted, not everywhere: skip grounding for small or familiar edits and for files
  already in context — grep is cheaper and Voyage is rate-limited (3 RPM / 10K TPM).
  Reach for reviewer when a change crosses files or touches a shared signature.
- Honesty about freshness: the base index tracks the target branch (base:<branch>),
  NOT your working tree; there is no working-tree overlay for local WIP. Grounding is
  reliable for facts about existing code (planning, callers of an unchanged symbol);
  it is blind to symbols you just edited locally — verify those with Read.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/skills/test_common_blocks.py -q`
Expected: PASS (все тесты, включая `test_common_files_have_no_include_markers` по glob).

- [ ] **Step 5: Commit**

```bash
git add plugin/skills/_common/reviewer-grounding.md tests/skills/test_common_blocks.py
git commit -m "feat(skills): общий блок reviewer-grounding для фаз план/ревью"
```

---

### Task 2: Session-less грунтовка в standalone `maintainability-review` + `performance-review`

**Files:**
- Modify: `plugin/skills/maintainability-review/SKILL.md:10-11`
- Modify: `plugin/skills/performance-review/SKILL.md:10-11`
- Test: `tests/skills/test_assembled_prompts.py:61-71` (расширить)

**Interfaces:**
- Consumes: `_common/reviewer-grounding.md` из Task 1 (маркер include).
- Produces: собранные промпты `maintainability-review/SKILL.md` и `performance-review/SKILL.md` содержат текст reviewer-grounding.

- [ ] **Step 1: Write the failing tests**

В `tests/skills/test_assembled_prompts.py` дополнить существующие тесты ассертами грунтовки:

```python
def test_performance_assembled_schema_and_goal():
    p = assemble("performance-review/SKILL.md")
    assert '"category": "performance"' in p
    assert '"confidence": 0.0' in p                 # из findings-schema
    assert "N+1" in p                               # perf-специфичный хвост остался
    assert "Reviewer grounding (optional, fail-open)" in p   # reviewer-grounding подставлен
    assert "search_codebase" in p                   # session-less тул для standalone


def test_maintainability_assembled_schema_and_whatnot():
    m = assemble("maintainability-review/SKILL.md")
    assert '"confidence": 0.0' in m                 # из findings-schema
    assert "What Not To Flag" in m                  # maint-специфичный хвост остался
    assert "Reviewer grounding (optional, fail-open)" in m   # reviewer-grounding подставлен
    assert "search_codebase" in m                   # session-less тул для standalone
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/skills/test_assembled_prompts.py -q`
Expected: FAIL — `test_performance_assembled_schema_and_goal` и `test_maintainability_assembled_schema_and_whatnot` падают на `"Reviewer grounding..."` (блок ещё не подключён).

- [ ] **Step 3: Edit both SKILL.md**

В `plugin/skills/maintainability-review/SKILL.md` заменить строки 10-11:

```
<!-- include: _common/tool-usage.md -->
Use the PR-session tools above.
```

на:

```
<!-- include: _common/tool-usage.md -->
<!-- include: _common/reviewer-grounding.md -->
In `/reviewer_review-pr` use the PR-session tools above. Standalone (no PR session): use
the session-less tools per the reviewer-grounding block when reviewer is connected and the
index is fresh; otherwise fall back to grep/Read.
```

В `plugin/skills/performance-review/SKILL.md` сделать **идентичную** замену тех же строк 10-11 (файлы в этом месте совпадают).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/skills/test_assembled_prompts.py tests/skills/test_common_blocks.py -q`
Expected: PASS (новые ассерты + все прежние: `N+1`, `What Not To Flag`, `confidence`, отсутствие неразрешённых include).

- [ ] **Step 5: Commit**

```bash
git add plugin/skills/maintainability-review/SKILL.md plugin/skills/performance-review/SKILL.md tests/skills/test_assembled_prompts.py
git commit -m "feat(skills): session-less грунтовка в standalone maintainability/performance-review"
```

---

### Task 3: Opt-in блок в README (EN + RU) + указатель плагина

**Files:**
- Modify: `README.md` (новый раздел перед `## CLI reference` (README.md:539) + запись в ToC около README.md:27-28)
- Modify: `README.ru.md` (новый раздел + запись в ToC около README.ru.md:20)
- Modify: `plugin/README.md` (короткий указатель)
- Test: `tests/skills/test_readme_grounding_block.py` (создать)

**Interfaces:**
- Produces: разделы «Reviewer grounding in plan/review phases (optional)» (EN) и «Грунтовка reviewer в фазах план/ревью (опционально)» (RU) с копипаст-блоком-цитатой для контекст-файла пользователя.

- [ ] **Step 1: Write the failing test**

Создать `tests/skills/test_readme_grounding_block.py`:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_readme_en_has_grounding_section():
    text = _read("README.md")
    assert "## Reviewer grounding in plan/review phases (optional)" in text
    assert "Reviewer grounding (plan/review, optional, fail-open)" in text  # копипаст-блок
    assert "search_codebase" in text and "callers" in text
    assert "drift == 0" in text
    assert "[Reviewer grounding in plan/review phases]" in text             # запись в ToC


def test_readme_ru_has_grounding_section():
    text = _read("README.ru.md")
    assert "## Грунтовка reviewer в фазах план/ревью (опционально)" in text
    assert "Грунтовка reviewer (план/ревью, опционально, fail-open)" in text  # копипаст-блок
    assert "search_codebase" in text and "callers" in text


def test_plugin_readme_points_to_grounding():
    text = _read("plugin/README.md")
    assert "грунтов" in text.lower()  # указатель на раздел грунтовки
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/skills/test_readme_grounding_block.py -q`
Expected: FAIL — разделов/указателя ещё нет.

- [ ] **Step 3: Add the sections**

**(a)** В `README.md` в блок Table of contents (после строки `- [CLI reference](#cli-reference)`, README.md:28) добавить строку:

```
- [Reviewer grounding in plan/review phases](#reviewer-grounding-in-planreview-phases-optional)
```

**(b)** В `README.md` перед `## CLI reference` (README.md:539) вставить раздел:

```
## Reviewer grounding in plan/review phases (optional)

The reviewer MCP tools are available in every phase, not only inside a PR review. If you
run a plan/review workflow (e.g. Superpowers' writing-plans, or any code-review step), you
can have the agent ground its work in the RAG + code graph instead of raw grep. This is
opt-in: paste the block below into your agent context file (CLAUDE.md / AGENTS.md /
GEMINI.md / .cursorrules — whichever your client uses).

> **Reviewer grounding (plan/review, optional, fail-open).** When the reviewer MCP is
> connected and its base index is fresh (`reviewer status --json` -> `drift == 0`), prefer the
> session-less reviewer tools over grep to ground cross-file facts during planning and review:
> `search_codebase` (relevant code), `callers` (blast-radius of a signature you are about to
> change), `related_symbols`, `definition`. Be targeted — skip small/familiar edits and files
> already in context (Voyage is rate-limited). The base index tracks the target branch, not
> your working tree: grounding is reliable for existing code but blind to symbols you just
> edited locally — verify those with Read. If reviewer is absent or the index is stale, fall
> back to grep/Read.

---

```

**(c)** В `README.ru.md` в Table of contents (после строки ToC около README.ru.md:20) добавить строку:

```
- [Грунтовка reviewer в фазах план/ревью](#грунтовка-reviewer-в-фазах-планревью-опционально)
```

**(d)** В `README.ru.md` перед разделом `## Политика per-repo и доска задач` (README.ru.md:763) вставить раздел:

```
## Грунтовка reviewer в фазах план/ревью (опционально)

Тулы reviewer-MCP доступны в любой фазе, не только внутри ревью PR. Если вы работаете по
конвейеру план/ревью (например, writing-plans из Superpowers или любой шаг code-review),
можно заставить агента грунтовать работу в RAG + графе кода вместо голого grep. Это opt-in:
вставьте блок ниже в свой контекст-файл (CLAUDE.md / AGENTS.md / GEMINI.md / .cursorrules — по
вашему клиенту).

> **Грунтовка reviewer (план/ревью, опционально, fail-open).** Когда reviewer-MCP подключён и
> его base-индекс свеж (`reviewer status --json` -> `drift == 0`), в фазах планирования и ревью
> предпочитай session-less тулы reviewer голому grep для кросс-файловых фактов: `search_codebase`
> (релевантный код), `callers` (blast-radius сигнатуры, которую собираешься менять),
> `related_symbols`, `definition`. Точечно — пропускай мелкие/знакомые правки и файлы, уже в
> контексте (Voyage rate-limited). Base-индекс отслеживает целевую ветку, не твоё рабочее дерево:
> грунтовка надёжна для существующего кода, но слепа к символам, которые ты только что правил
> локально — их проверяй через Read. Если reviewer недоступен или индекс устарел — откат в grep/Read.

---

```

**(e)** В `plugin/README.md` после раздела «## Установка плагина» (plugin/README.md:37, перед `## Headless`) добавить:

```
## Грунтовка в план/ревью (опц.)

Reviewer-тулы доступны не только в ревью PR — их можно включить в фазах планирования/ревью
(writing-plans и т.п.), вставив opt-in блок в свой контекст-файл. См.
[README.md](../README.md#reviewer-grounding-in-planreview-phases-optional) (EN) /
[README.ru.md](../README.ru.md#грунтовка-reviewer-в-фазах-планревью-опционально) (RU).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/skills/test_readme_grounding_block.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md README.ru.md plugin/README.md tests/skills/test_readme_grounding_block.py
git commit -m "docs(readme): opt-in блок reviewer-грунтовки (EN/RU) + указатель плагина"
```

---

### Task 4: Догфуд — RU-блок грунтовки в `CLAUDE.md` этого репо

**Files:**
- Modify: `CLAUDE.md` (новый раздел после `## Соглашения`, в конце файла)

**Interfaces:**
- Consumes: текст RU-блока из Task 3 (тот же копипаст-блок).

- [ ] **Step 1: Add the grounding section to CLAUDE.md**

В конец `CLAUDE.md` (после раздела `## Соглашения`) добавить:

```
## Грунтовка reviewer в фазах план/ревью (опционально)

Догфуд PRI-203. В фазах планирования/ревью, если reviewer-MCP подключён и его base-индекс
свеж (`reviewer status --json` -> `drift == 0`), предпочитай session-less тулы reviewer
голому grep для кросс-файловых фактов: `search_codebase` (релевантный код), `callers`
(blast-radius сигнатуры, которую собираешься менять), `related_symbols`, `definition`.
Точечно — пропускай мелкие/знакомые правки и файлы, уже в контексте (Voyage 3 RPM / 10K TPM).
Base-индекс отслеживает целевую ветку, не рабочее дерево: грунтовка надёжна для существующего
кода, но слепа к символам, только что правленным локально — их проверяй через Read. Если
reviewer недоступен или индекс устарел — откат в grep/Read.
```

- [ ] **Step 2: Verify the edit landed**

Run: `grep -c "Грунтовка reviewer в фазах план/ревью" CLAUDE.md`
Expected: `1`

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): догфуд reviewer-грунтовки в CLAUDE.md"
```

---

### Task 5: Финальная проверка (guard-sweep + ruff)

**Files:** (нет правок кода; только прогон)

- [ ] **Step 1: Run the full skills guard suite**

Run: `.venv/bin/pytest -q tests/skills`
Expected: PASS (все тесты; никаких неразрешённых include, все ассерты грунтовки зелёные).

- [ ] **Step 2: Lint changed files**

Run: `.venv/bin/ruff check tests/skills/test_common_blocks.py tests/skills/test_assembled_prompts.py tests/skills/test_readme_grounding_block.py`
Expected: `All checks passed!` (правились только .md + тесты; при линт-замечаниях в новых тестах — исправить и перезапустить).

- [ ] **Step 3: Sanity — весь юнит-прогон не сломан сменой скилов**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключён по умолчанию; правки — только md/тесты, движок не тронут).

## Self-Review

**1. Spec coverage:**
- `_common/reviewer-grounding.md` → Task 1. ✅
- Грунтовка maintainability/performance (standalone session-less, fail-open) → Task 2. ✅
- Opt-in README EN+RU + указатель плагина → Task 3. ✅
- Догфуд CLAUDE.md → Task 4. ✅
- Решение «session-less impact НЕ делаем» → зафиксировано в спеке + блоке (честность WIP/base в Task 1); кода не требует. ✅
- Тесты (инварианты блока + ассерт подключения + README-guard) → Task 1/2/3; финальный sweep → Task 5. ✅
- `review-pr`/`ask`/`solve-task` не трогаем → ни одна задача их не меняет. ✅

**2. Placeholder scan:** плейсхолдеров нет — весь контент блоков/разделов/тестов приведён verbatim.

**3. Type consistency:** ассерт-строки согласованы между задачами — блок содержит `"Reviewer grounding (optional, fail-open)"` (Task 1 создаёт, Task 2 ассертит в сборке); README-заголовки EN/RU совпадают между Task 3 (создание) и тестом Task 3. Кортеж `_common` в Task 1 включает `"reviewer-grounding.md"` — файл создаётся в том же Task 1.
