# PRI-208 — Выбор модели для сборки брифа (solve-task) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать `solve-task` спрашивать у юзера, на какой (более дешёвой) модели собрать бриф, рекомендуя mid/Sonnet-класс, кросс-CLI — и исполнять шаги 2–4 на выбранной модели (subagent где есть override, иначе inline).

**Architecture:** Правка одного Claude-Code-скилла `plugin/skills/solve-task/SKILL.md` (markdown): новый Step 1.5 «Choose the brief model» + wrapper над шагами 2–4 (пути A=subagent-on-chosen-model / B=inline-fallback + строка-маркер «Собран на: …»). Поведение фиксируется guard-тестами, читающими текст `SKILL.md` (как `tests/skills/test_summarize_subsystems.py`). Python-код и хук `brief_cost.py` не трогаем. Плюс синк README EN+RU.

**Tech Stack:** Markdown-скиллы Claude Code; pytest (guard-тесты в `tests/skills/`, читают файл как текст, без БД/сети).

## Global Constraints

- Работать на фиче-ветке от `dev` (напр. `feat/pri-208-brief-model-choice`); `dev` защищена, интеграция через PR. Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/Claude).
- Тело `SKILL.md` — по-английски; текст инструктирует агента **общаться с юзером по-русски** (как весь скилл).
- **НЕ трогать** `plugin/hooks/brief_cost.py`, `tests/hooks/*`, `plugin/skills/summarize-subsystems/*`, общий `_common/` (решение brainstorming: best-effort учёт токенов, solve-task-local).
- **НЕ удалять** существующие anchor-фразы solve-task guard-тестов при правке `SKILL.md` (см. Task 1, шаг 0).
- Прогон тестов: `.venv/bin/pytest tests/skills/ -q` (быстрые, без интеграции).
- Дефолт-рекомендация tier — **mid / Sonnet-класс**; Fable не рекомендовать.

---

### Task 1: Step 1.5 «Choose the brief model» + brief-building-unit wrapper в SKILL.md (TDD)

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (вставка после Step 1 «Config», перед Step 2)
- Test: `tests/skills/test_solve_task_brief.py` (добавить 3 теста)

**Interfaces:**
- Consumes: ничего от других задач.
- Produces: уникальные фразы-маркеры в `SKILL.md`, на которые ассертят guard-тесты:
  - `"Ask the user which model tier to use for building the brief"`
  - `"mid tier (Sonnet-class)"`
  - `"dispatch a subagent on the chosen model"`
  - `"per-subagent model override unavailable"`
  - `"Собран на"` (строка-маркер, которую скилл дописывает в бриф)

- [ ] **Step 0: Зафиксировать существующие anchor-фразы (регрессия).** Перед правкой убедиться, что правка НЕ удалит фразы, которые уже проверяют другие тесты в `tests/skills/test_solve_task_brief.py`. Прогнать текущий набор — он должен быть зелёным ДО изменений:

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py -q`
Expected: PASS (все существующие тесты зелёные). Эти фразы (`# Brief —`, `No fixed ceilings`, `bounded server-side`, `(dropped`, `directly informs`, `project=`, `task_board.project`, `docs/superpowers/briefs/`, `Persist the brief`, `file path`, `Board-less`, `Dedup related sources by key`, `linked ∪ similar`, `canonical task key`, `Thin-criteria enrichment`, `include_tests=True`, `Test exemplars`, `Lazy expansion (no user prompt)`, `top_k=`) вставку Step 1.5 переживают — вставляем НОВЫЙ шаг, ничего не удаляя.

- [ ] **Step 1: Написать падающие guard-тесты.** Добавить в конец `tests/skills/test_solve_task_brief.py`:

```python
def test_solve_task_asks_brief_model_choice():
    """Новый Step 1.5 должен спрашивать у юзера tier модели для сборки брифа."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "Ask the user which model tier to use for building the brief" in text, (
        "нет шага выбора модели для брифа (уникальная фраза шага удалена)"
    )
    # Рекомендация-дефолт — mid/Sonnet-класс
    assert "mid tier (Sonnet-class)" in text, (
        "скилл не рекомендует mid/Sonnet-класс как дефолт"
    )


def test_solve_task_dispatches_brief_subagent_on_chosen_model():
    """Путь A: шаги 2–4 диспатчатся сабагентом на выбранной модели; путь B — inline-фолбэк."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "dispatch a subagent on the chosen model" in text, (
        "нет диспатча сабагента на выбранной модели (путь A)"
    )
    assert "per-subagent model override unavailable" in text, (
        "нет inline-фолбэка для CLI без per-subagent override (путь B)"
    )


def test_solve_task_records_brief_model_marker():
    """Оркестратор дописывает в бриф строку-маркер «Собран на: …»."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "Собран на" in text, (
        "нет строки-маркера «Собран на: <tier/модель>» (наблюдаемость выбора)"
    )
```

- [ ] **Step 2: Прогнать новые тесты — убедиться, что падают.**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py -q -k "brief_model_choice or brief_subagent or brief_model_marker"`
Expected: FAIL (3 теста) — «уникальная фраза шага удалена» / AssertionError (фраз ещё нет в `SKILL.md`).

- [ ] **Step 3: Вставить новый Step 1.5 в `SKILL.md`.** Найти в `plugin/skills/solve-task/SKILL.md` конец шага `1. **Config.** …` (он заканчивается строкой про board-less mode: «… → board-less mode (continue without it).») и **сразу после него, перед строкой `2. **Identify the task.**`** вставить:

```markdown
1.5. **Choose the brief model (cross-CLI).** Building the brief (Steps 2–4: gather + distill) is a
   light reasoning task over session-less retrieval tools — a top-tier model is overkill and burns
   tokens. Before building it, **Ask the user which model tier to use for building the brief**,
   phrasing the choice by **tier (cheap / mid / premium)** — not by concrete model names — so it
   works across CLIs (Claude Code, Codex, Gemini, Cursor, …). **Recommend a mid tier (Sonnet-class)
   as the default** (do not recommend Fable — a coarse tier is fine but the brief still needs sound
   judgment). Talk to the user in Russian. Remember the choice for this run. Fail-open: no answer or
   a decline → use the default tier (or, on Path B below, the session model inline). Never block.
```

- [ ] **Step 4: Вставить brief-building-unit wrapper перед Step 2.** Сразу после вставленного Step 1.5 (и перед `2. **Identify the task.**`) добавить:

```markdown
**Brief-building unit (Steps 2–4) runs on the chosen model.** Steps 2–4 (identify → gather → distill
→ persist) are non-interactive; run them on the model chosen in Step 1.5:
- **Path A — per-subagent model override available:** **dispatch a subagent on the chosen model** to
  execute Steps 2–4, giving it the reviewer session-less tools (`get_task`, `search_codebase`,
  `get_subsystem_summaries`, `get_task_context`, `search_tasks`, the graph tools, `get_pr_diff`) plus
  the harness `Read`/`Bash`/`Glob`/`Write` (to persist the brief). The subagent returns the brief file
  path and a short summary (kept / dropped).
- **Path B — per-subagent model override unavailable** (some CLIs): build the brief **inline** on the
  session model, or offer the escape-hatch «switch model / run it yourself» in the spirit of the
  preflight «Прогрею сам» option (Step 0.4). Note in the report that the brief was built inline.
- **Existing-artifacts warn** (Step 4, user-facing «warn, don't block»): the **orchestrator** runs
  that scan-and-warn **before dispatch** (a subagent must not prompt the user); the idempotency
  overwrite-glob stays inside the subagent's persist.
- After the unit returns, the orchestrator **appends a marker line to the brief**:
  `Собран на: <tier/модель>, режим: subagent | inline` — records which model built the brief. The
  `brief_cost` token block is best-effort and may miss subagent sidechain tokens (documented limitation).
- Fail-open: an error or empty return from the subagent → the orchestrator finishes the brief inline
  on the session model. Model choice must never break the pipeline.
```

- [ ] **Step 5: Прогнать новые тесты — убедиться, что проходят.**

Run: `.venv/bin/pytest tests/skills/test_solve_task_brief.py -q -k "brief_model_choice or brief_subagent or brief_model_marker"`
Expected: PASS (3 теста).

- [ ] **Step 6: Прогнать весь набор скилл-тестов — регрессия (ничего не сломали).**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (все тесты, включая существующие solve-task/summarize/common/readme-grounding).

- [ ] **Step 7: Commit.**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_solve_task_brief.py
git commit -m "feat(skills): solve-task спрашивает модель для сборки брифа (кросс-CLI, PRI-208)"
```

---

### Task 2: Синк README EN + RU

**Files:**
- Modify: `README.md` (секция `### reviewer_solve-task — from task to implementation (killer feature)`, ~L629)
- Modify: `README.ru.md` (секция `### reviewer_solve-task — от задачи до реализации (ключевая фича)`, ~L558)

**Interfaces:**
- Consumes: поведение из Task 1 (выбор модели для брифа).
- Produces: doc-строки; тестами не покрывается (docs).

- [ ] **Step 1: Прочитать целевые секции, чтобы вставить строку в существующий стиль.**

Run: `sed -n '629,700p' README.md` и `sed -n '558,630p' README.ru.md`
Expected: увидеть буллеты/абзацы описания `solve-task`, куда логично добавить строку про выбор модели.

- [ ] **Step 2: Добавить строку в `README.md` (EN)** в секцию solve-task — например, отдельным буллетом в описании пайплайна:

```markdown
- **Cheaper model for the brief (cross-CLI).** Before building the brief, `solve-task` asks which
  model tier to run it on (by tier — cheap / mid / premium — not by model name, so it works across
  CLIs) and recommends a mid (Sonnet-class) default: gathering and distilling the brief is light
  reasoning, so a top-tier model is overkill. Where the harness supports per-subagent model override
  it dispatches the brief-building on the chosen model; otherwise it builds inline.
```

- [ ] **Step 3: Добавить зеркальную строку в `README.ru.md` (RU)** в секцию solve-task:

```markdown
- **Дешевле модель под бриф (кросс-CLI).** Перед сборкой брифа `solve-task` спрашивает, на каком
  tier'е модели его собрать (по tier'ам — cheap / mid / premium, а не по имени модели, чтобы работало
  в разных CLI) и рекомендует mid (Sonnet-класс) по умолчанию: сбор и распил брифа — лёгкий reasoning,
  топ-модель избыточна. Где харнесс умеет per-subagent override — сборка брифа идёт на выбранной
  модели; иначе — inline.
```

- [ ] **Step 4: Прогнать readme-grounding guard (не сломали блок).**

Run: `.venv/bin/pytest tests/skills/test_readme_grounding_block.py -q`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add README.md README.ru.md
git commit -m "docs: README (EN+RU) — solve-task выбирает модель для сборки брифа (PRI-208)"
```

---

## Self-Review (выполнено при написании плана)

- **Spec coverage:** Step 1.5 (выбор модели, дефолт mid/Sonnet, кросс-CLI по tier'ам) → Task 1 Step 3; пути A/B (subagent/inline) + маркер «Собран на» → Task 1 Step 4; guard-тест → Task 1 Steps 1–2; README EN+RU → Task 2; fail-open → включён в текст шагов. Хук `brief_cost`/`summarize`/`_common` — явно вне скоупа (Global Constraints). Пробелов нет.
- **Placeholder scan:** плейсхолдеров нет — весь текст `SKILL.md`/README/тестов приведён дословно.
- **Type/phrase consistency:** уникальные фразы в `SKILL.md` (Step 3/4) дословно совпадают с ассертами guard-тестов (Step 1): `"Ask the user which model tier to use for building the brief"`, `"mid tier (Sonnet-class)"`, `"dispatch a subagent on the chosen model"`, `"per-subagent model override unavailable"`, `"Собран на"`.
