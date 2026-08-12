# PRI-243 — режим взаимодействия и стратегия исполнения в solve-task: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Спека: `docs/superpowers/specs/2026-08-12-pri-243-solve-task-interaction-mode-design.md`
Бриф: `docs/superpowers/briefs/2026-08-12-PRI-243-solve-task-interaction-mode-and-execution-strategy.md`

**Goal:** Добавить в скилл `solve-task` один стартовый опрос (тир модели брифа + режим
взаимодействия + стратегия исполнения), профиль `lite` поверх SDD и git-ignored носитель выбора,
переживающий компакцию контекста.

**Architecture:** Правка касается только промптов плагина и guard-тестов. Новый подпункт `0.
Startup survey` внутри существующего Шага 0 задаёт одну панель `AskUserQuestion` с тремя вопросами;
выбор персистится в `.superpowers/solve-task/<KEY>.md` (git-ignored) и передаётся дальше явной
директивой на хендоффе. Профиль `plugin/skills/_profiles/execution-lite.md` описывает три дельты к
`superpowers:subagent-driven-development` и ничего не переопределяет сам.

**Tech Stack:** Markdown-промпты плагина Claude Code, pytest (маркерные guard-тесты над markdown),
`scripts/update_codex_plugin_manifest.py`.

## Global Constraints

- Тело `SKILL.md` и профиля — **на английском** (экономия токенов); строки, обращённые к
  пользователю, внутри них — на русском. Ответы пользователю скилл даёт по-русски.
- **Нумерацию пайплайна `SKILL.md` не менять.** Шаг 0 переименовывается в
  `0. **Startup: survey + Preflight (index freshness + task-corpus warm-up).**` — слово `Preflight`
  с заглавной буквы обязано остаться в заголовке, иначе падает
  `tests/skills/test_preflight_guardrail.py::test_solve_task_has_preflight` (это единственное
  вхождение `Preflight` с заглавной во всём файле). Внутрь добавляется подпункт `0. **Startup survey.**`
  ПЕРЕД существующим `1. **Base-index freshness.**`. Подпункты 1–4 сохраняют свои номера, все
  ссылки вида `Step 0.1`, `Step 0.4`, `Steps 2–4`, `Step 3 contract`, `Step 4 filter`, а также
  заголовки `1. **Config.**`, `2. **Identify the task.**`, `3. **Gather context**`,
  `4. **Distill the solution brief.**`, `5. **Hand off to development.**` остаются как есть.
- Шаг `1.5. **Choose the brief model (cross-CLI).**` **удаляется целиком**, его содержание
  переезжает в стартовый опрос. Блок `**Brief-building unit (Steps 2–4) runs on the chosen model.**`
  остаётся на месте, ссылка «chosen in Step 1.5» в нём заменяется на «chosen in the Step 0 startup
  survey».
- Скоуп файлов: `plugin/skills/solve-task/SKILL.md`, новый `plugin/skills/_profiles/execution-lite.md`,
  новый `tests/skills/test_solve_task_modes.py`, `README.md`, `README.ru.md`, сгенерированные
  манифесты. Сервер, `Settings`, `.review.yml`, разбор `$ARGUMENTS` — **не трогать**.
- Дефолты при отсутствии ответа: тир `mid`, режим `normal`, стратегия `subagent`. Пайплайн не
  блокируется никогда.
- Ни бриф, ни спека, ни план не должны содержать инструкций записывать в них режим или перечень
  решений, принятых за пользователя.
- Тесты гоняются как `.venv/bin/pytest -q`. Существующие `tests/skills/test_preflight_guardrail.py`
  и `tests/skills/test_solve_task_brief.py` должны остаться зелёными без правок.
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`,
  упоминаний Claude).
- Ветка: `feat/pri-243-solve-task-modes`.

---

### Task 1: Профиль `execution-lite` и его guard-тесты

Создаёт новую директорию `_profiles/` и файл-профиль, а также заводит тестовый модуль, который
дальше пополняют задачи 2 и 3.

**Files:**
- Create: `plugin/skills/_profiles/execution-lite.md`
- Create: `tests/skills/test_solve_task_modes.py`

**Interfaces:**
- Consumes: ничего.
- Produces: путь `plugin/skills/_profiles/execution-lite.md` (задача 3 записывает его абсолютную
  форму в файл прогона); тестовый модуль `tests/skills/test_solve_task_modes.py` с константами
  `ROOT`, `SKILL`, `PROFILE`, который задачи 2 и 3 дополняют новыми функциями.

- [ ] **Step 1: Написать падающие тесты профиля**

Создать `tests/skills/test_solve_task_modes.py`:

```python
"""Guardrail: стартовый опрос solve-task и профиль исполнения lite (PRI-243).

Тесты маркерные: пинят стабильные якоря спеки, а не формулировки, чтобы правка
промпта не удалила требование молча.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"
PROFILE = ROOT / "plugin" / "skills" / "_profiles" / "execution-lite.md"


def test_lite_profile_exists():
    assert PROFILE.is_file(), "нет plugin/skills/_profiles/execution-lite.md"
    assert PROFILE.read_text(encoding="utf-8").strip()


def test_lite_profile_is_a_delta_over_sdd():
    text = PROFILE.read_text(encoding="utf-8")
    assert "superpowers:subagent-driven-development" in text


def test_lite_profile_groups_reviews():
    text = PROFILE.read_text(encoding="utf-8")
    assert "at most 3" in text            # потолок размера группы
    assert "overlapping files" in text    # критерий склейки задач в группу


def test_lite_profile_lowers_fix_round_cap():
    text = PROFILE.read_text(encoding="utf-8")
    assert "3 fix rounds" in text
    assert "instead of 5" in text
    assert "round 3" in text              # эскалация модели сдвинута на раунд 3


def test_lite_profile_keeps_final_review_mandatory():
    text = PROFILE.read_text(encoding="utf-8")
    assert "final whole-branch review" in text
    assert "mandatory" in text
    assert "never disabled" in text


def test_lite_profile_has_no_own_machinery():
    # Профиль — список дельт, а не исполнитель: он не переопределяет ledger,
    # BASE-трекинг и собственный цикл, а ссылается на SDD.
    text = PROFILE.read_text(encoding="utf-8")
    assert "# SDD ledger" not in text        # формат ledger не переопределяется
    assert "git rev-parse HEAD" not in text  # рецепт BASE-трекинга не дублируется
    assert "unchanged from superpowers:subagent-driven-development" in text
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/skills/test_solve_task_modes.py -q`
Expected: FAIL — `test_lite_profile_exists` падает с «нет plugin/skills/_profiles/execution-lite.md»,
остальные — с `FileNotFoundError`.

- [ ] **Step 3: Создать профиль**

Создать `plugin/skills/_profiles/execution-lite.md`:

```markdown
# Execution profile: lite

A short list of deltas over `superpowers:subagent-driven-development` (SDD). This file is a
profile, not an executor: it defines no loop, no ledger format and no BASE tracking of its own.
Everything not listed below — TDD, the ledger, BASE tracking, model selection, dispatch prompt
templates, the breaker rules — is unchanged from superpowers:subagent-driven-development.

Use it when the startup survey of `rag-reviewer:solve-task` selected the `lite` strategy, or when
the `auto` rubric resolved to `lite`.

## Delta 1 — review per group, not per task

SDD dispatches a task reviewer after every task. In `lite`, the reviewer is dispatched once per
**group**.

A group is a run of consecutive plan tasks that touch overlapping files, **at most 3** tasks long.
Tasks whose files do not overlap are never merged into one group, even when they are adjacent and
the group is short.

Dispatch the reviewer on the diff of the whole group. `BASE` is the commit recorded before the
group's first task — not `HEAD~1`, and not the base of the last task in the group.

A group that fails review enters the fix loop as one unit: the findings name the task they belong
to, and the fix dispatch resumes the implementer of that task.

## Delta 2 — fix-round cap of 3

The per-group fix loop allows **3 fix rounds** `instead of 5`. Model escalation moves accordingly:
a fresh implementer on a more capable model is dispatched from `round 3` instead of round 4.

The breaker is unchanged: at the cap, adjudicate every open finding yourself — park it with a
ruling, or stop and report BLOCKED when it is real and load-bearing.

## Delta 3 — the final review stays

The `final whole-branch review` is `mandatory` under this profile and is `never disabled`, in any
interaction mode, including `full-auto`. It is the only broad check in the run, and `lite` has
increased what rides on it: per-task gates were traded for per-group ones.

Dispatch it exactly as SDD prescribes — most capable available model, whole-branch review package,
pointed at the ledger's deferred-minor and parked lines.

## What this profile does not change

Ledger bookkeeping, BASE recording, worktree setup, implementer and reviewer prompt templates,
model selection rules, and the finish sequence are `unchanged from superpowers:subagent-driven-development`.
Read that skill for all of them.
```

- [ ] **Step 4: Убедиться, что тесты профиля проходят**

Run: `.venv/bin/pytest tests/skills/test_solve_task_modes.py -q`
Expected: PASS (6 тестов).

- [ ] **Step 5: Проверить, что существующие тесты скиллов не сломались**

Run: `.venv/bin/pytest tests/skills -q`
Expected: PASS. Профиль лежит в `plugin/skills/_profiles/`, у него нет `SKILL.md`, поэтому
`test_skill_names.py` его не регистрирует; `test_assembled_prompts.py::test_public_skill_markdown_has_no_provider_specific_board_surface`
проходит по нему `rglob("*.md")` и не должен найти provider-specific лексики.

- [ ] **Step 6: Коммит**

```bash
git add plugin/skills/_profiles/execution-lite.md tests/skills/test_solve_task_modes.py
git commit -m "feat(plugin): профиль исполнения lite поверх subagent-driven-development"
```

---

### Task 2: Стартовый опрос в `SKILL.md`

Добавляет панель из трёх вопросов, удаляет Step 1.5 и подчиняет предполётные вопросы режиму.

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (заголовок Шага 0; новый подпункт `0.`; удаление
  Step 1.5; правка ссылки в блоке `**Brief-building unit …**`)
- Modify: `tests/skills/test_solve_task_modes.py` (дописать тесты в конец)

**Interfaces:**
- Consumes: файл `tests/skills/test_solve_task_modes.py` с константами `ROOT`, `SKILL`, `PROFILE`
  из задачи 1.
- Produces: якоря в `SKILL.md`, на которые опирается задача 3 — заголовок подпункта
  `0. **Startup survey.**`, значения режимов `` `normal` ``, `` `auto` ``, `` `full-auto` ``,
  значения стратегий `` `inline` ``, `` `subagent` ``, `` `lite` ``, `` `auto` ``; хелпер
  `_survey_section()` в тестовом модуле.

- [ ] **Step 1: Написать падающие тесты опроса**

Дописать в конец `tests/skills/test_solve_task_modes.py`:

```python
def _survey_section() -> str:
    """Вырезать подпункт 0 Шага 0 — от заголовка опроса до пункта freshness."""
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("0. **Startup survey.**")
    return text[start:text.index("1. **Base-index freshness.**", start)]


def test_startup_survey_runs_before_preflight_checks():
    text = SKILL.read_text(encoding="utf-8")
    assert text.index("0. **Startup survey.**") < text.index("1. **Base-index freshness.**")
    assert text.index("0. **Startup survey.**") < text.index("3. **Warm the task corpus.**")


def test_survey_is_one_panel_with_three_questions():
    section = _survey_section()
    assert "AskUserQuestion" in section
    assert "one panel" in section
    # регистр важен: в промпте это заголовки вопросов с заглавной буквы
    assert "Brief model tier" in section
    assert "Interaction mode" in section
    assert "Execution strategy" in section


def test_survey_offers_three_interaction_modes_each_explained():
    section = _survey_section()
    for value in ("`normal`", "`auto`", "`full-auto`"):
        assert value in section, f"нет режима {value}"
    assert "explain what it means" in section   # пояснение обязательно у каждого значения


def test_survey_offers_four_execution_strategies():
    section = _survey_section()
    for value in ("`inline`", "`subagent`", "`lite`", "`auto`"):
        assert value in section, f"нет стратегии {value}"


def test_survey_defaults_are_fail_open():
    section = _survey_section()
    assert "never block" in section
    assert "`mid`" in section
    assert "defaults" in section
    assert "non-interactive" in section          # единственное исключение из показа панели


def test_auto_permission_mode_shortcut_removed():
    # Правило «в auto permission mode тир выбирается молча» удалено: панель
    # показывается всегда, кроме headless/non-interactive.
    text = SKILL.read_text(encoding="utf-8")
    assert "auto permission mode" not in text
    assert "1.5. **Choose the brief model" not in text


def test_brief_building_unit_points_at_the_survey():
    text = SKILL.read_text(encoding="utf-8")
    assert "chosen in Step 1.5" not in text
    assert "Step 0 startup survey" in text


def test_full_auto_suppresses_preflight_questions():
    text = SKILL.read_text(encoding="utf-8")
    assert "In `full-auto`, do not ask" in text
    assert "recommended option" in text
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/skills/test_solve_task_modes.py -q`
Expected: FAIL — `_survey_section` падает с `ValueError: substring not found` на
`0. **Startup survey.**`.

- [ ] **Step 3: Переименовать заголовок Шага 0**

В `plugin/skills/solve-task/SKILL.md` заменить строку заголовка

```
0. **Preflight (index freshness + task-corpus warm-up).** Run this BEFORE anything else.
```

на

```
0. **Startup: survey + Preflight (index freshness + task-corpus warm-up).** Run this BEFORE anything else.
```

Слово `Preflight` с заглавной буквы обязано остаться: это единственное такое вхождение в файле, и
на него завязан `tests/skills/test_preflight_guardrail.py::test_solve_task_has_preflight`.

Абзац, следующий за заголовком (резолв пути репо, рабочей ветки и `task_board`), не трогать.

- [ ] **Step 4: Вставить подпункт `0. Startup survey`**

Вставить ПЕРЕД существующей строкой `   1. **Base-index freshness.**` (с тем же отступом в три
пробела, что у соседних подпунктов):

```markdown
   0. **Startup survey.** Ask the user, in **one panel** (`AskUserQuestion`), three questions at
      once. This is the only survey of the run: none of the three is asked again later. Talk to the
      user in Russian.
      1. **Brief model tier** — `cheap` / `mid` (recommended) / `premium`. Phrase the choice by
         tier, not by concrete model names, so it works across CLIs (Claude Code, Codex, Gemini,
         Cursor, …). Do not recommend a coarse tier such as Fable — the brief still needs sound
         judgment. This question replaces the former Step 1.5.
      2. **Interaction mode** — three values; the option text must **explain what it means**:
         - `normal` — «вопросы на брейншторме, апрув спеки и апрув плана» (current behaviour);
         - `auto` — «вопросы задаются, апрувы спеки и плана не запрашиваются»;
         - `full-auto` — «вопросы не задаются, на каждой развилке берётся рекомендованный вариант,
           апрувы не запрашиваются». Add the cost to the same option text: «уместен для задач с
           полным описанием и критериями; для расплывчатых формулировок подавляет канал, по
           которому в дизайн попадает недостающая информация».
      3. **Execution strategy** — `inline` (superpowers:executing-plans), `subagent`
         (superpowers:subagent-driven-development as-is), `lite` (the profile at
         `_profiles/execution-lite.md`), `auto` (resolved by the rubric in Step 5 after the plan is
         written). Asked now, applied later.

      **Defaults (fail-open).** No answer, a decline, or a headless / `non-interactive` run → tier
      `mid`, mode `normal`, strategy `subagent`. In a headless / `non-interactive` run do not show
      the panel at all and apply those defaults silently. Otherwise the panel is always shown.
      **never block** — the survey must not stop the pipeline under any circumstance.

      **The mode governs the preflight questions below.** In `full-auto`, do not ask the
      confirmations of steps 1 and 4 (stale index, missing summaries): take the **recommended
      option** in each (reindex; warm the summaries) and record each one as a decision made on the
      user's behalf, per Step 4's run-state file. In `normal` and `auto`, ask them as written.
```

- [ ] **Step 5: Удалить Step 1.5 и починить ссылку на него**

Удалить целиком блок, начинающийся строкой `1.5. **Choose the brief model (cross-CLI).**` (весь
абзац до пустой строки перед `**Brief-building unit (Steps 2–4) runs on the chosen model.**`).

В оставшемся блоке заменить

```
non-interactive; run them on the model chosen in Step 1.5:
```

на

```
non-interactive; run them on the model chosen in the Step 0 startup survey:
```

Проверить весь файл на другие вхождения `Step 1.5` и заменить их той же формулировкой:

Run: `grep -n "Step 1.5\|1\.5\." plugin/skills/solve-task/SKILL.md`
Expected: пусто.

- [ ] **Step 6: Прогнать тесты**

Run: `.venv/bin/pytest tests/skills -q`
Expected: PASS — новые тесты опроса зелёные, `test_preflight_guardrail.py` и
`test_solve_task_brief.py` не сломаны (нумерация подпунктов 1–4 и заголовков 1–5 не менялась).

- [ ] **Step 7: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_solve_task_modes.py
git commit -m "feat(plugin): стартовый опрос solve-task — режим, стратегия и тир модели"
```

---

### Task 3: Носитель выбора, границы `full-auto` и директивы хендоффа

Персист выбора в git-ignored файл прогона, рубрика стратегии `auto`, именованный список
подтверждений и передача выбора дальше как воли пользователя.

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (новый подраздел в шаге 4 «Distill the solution
  brief»; правка шага `5. **Hand off to development.**`)
- Modify: `tests/skills/test_solve_task_modes.py` (дописать тесты в конец)

**Interfaces:**
- Consumes: якоря задачи 2 (`0. **Startup survey.**`, значения режимов и стратегий) и путь профиля
  из задачи 1.
- Produces: якоря `Persist the run state`, `.superpowers/solve-task/`, `Task Right-Sizing`,
  рубрику `auto` — их проверяет задача 4 через README.

- [ ] **Step 1: Написать падающие тесты**

Дописать в конец `tests/skills/test_solve_task_modes.py`:

```python
def _run_state_section() -> str:
    """Вырезать подраздел персиста выбора — от его заголовка до хендоффа."""
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("**Persist the run state")
    return text[start:text.index("5. **Hand off to development.**", start)]


def _brief_persist_section() -> str:
    """Вырезать подраздел персиста брифа — он не должен нести режим."""
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("**Persist the brief (survivability).**")
    return text[start:text.index("**Persist the run state", start)]


def _handoff_section() -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("5. **Hand off to development.**")
    return text[start:text.index("## Failure handling", start)]


def test_run_state_lives_in_gitignored_dir():
    section = _run_state_section()
    assert ".superpowers/solve-task/" in section
    assert "<KEY>.md" in section
    assert "git-ignored" in section


def test_run_state_records_mode_strategy_and_profile_path():
    section = _run_state_section()
    assert "Режим:" in section
    assert "Стратегия:" in section
    assert "_profiles/execution-lite.md" in section
    assert "absolute" in section          # путь профиля пишется абсолютным


def test_decisions_section_only_in_full_auto():
    section = _run_state_section()
    assert "Решения, принятые за пользователя" in section
    assert "only in `full-auto`" in section


def test_mode_never_written_into_committed_artifacts():
    # Спека и план коммитятся: ни режим, ни перечень решений туда не пишутся.
    brief_section = _brief_persist_section()
    assert "full-auto" not in brief_section
    assert "Режим" not in brief_section
    run_state = _run_state_section()
    assert "never write the mode" in run_state
    assert "spec" in run_state and "plan" in run_state


def test_full_auto_confirmation_boundary_is_a_named_list():
    text = SKILL.read_text(encoding="utf-8")
    assert "git push" in text
    assert "creating a PR" in text
    assert "board write" in text


def test_auto_strategy_rubric_has_observable_thresholds():
    section = _handoff_section()
    assert "> 8 tasks" in section
    assert "> 10" in section
    assert "≤ 3 tasks" in section
    assert "first match wins" in section   # правила упорядочены, ветка ровно одна


def test_auto_rubric_names_risk_signals():
    section = _handoff_section()
    for signal in ("schema migration", "MCP tool", "credentials", "irreversible"):
        assert signal in section, f"нет рискового признака {signal}"


def test_handoff_passes_mode_as_user_instruction():
    section = _handoff_section()
    assert "the user's explicit instruction" in section
    assert "not a request to bypass" in section


def test_handoff_requires_task_right_sizing():
    section = _handoff_section()
    assert "Task Right-Sizing" in section


def test_handoff_carries_run_state_path_forward():
    section = _handoff_section()
    assert ".superpowers/solve-task/" in section
    assert "re-read" in section
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/skills/test_solve_task_modes.py -q`
Expected: FAIL — `_run_state_section` падает с `ValueError: substring not found` на
`**Persist the run state`.

- [ ] **Step 3: Добавить подраздел персиста выбора**

В `plugin/skills/solve-task/SKILL.md`, в шаге `4. **Distill the solution brief.**`, вставить ПОСЛЕ
блока `**Persist the brief (survivability).**` (после его последнего пункта `**Fail-open:** …`) и
ПЕРЕД строкой `5. **Hand off to development.**`:

```markdown
   **Persist the run state (mode + strategy).** The survey's answers must survive context
   compaction and two skill handoffs, but they must NOT land in a committed artifact: the spec and
   the plan end up in the PR, where a list of decisions made on the user's behalf reads as a
   receipt that nobody approved the design. So they go to a **git-ignored** run-state file instead.
   - **Path:** `.superpowers/solve-task/<KEY>.md` — board-less: `.superpowers/solve-task/<slug>.md`.
     `.superpowers/` is already git-ignored (it is where subagent-driven-development keeps its
     ledger). Create the directory if missing (`mkdir -p`). The path is derived from the task KEY,
     so any later step can rebuild it without remembering the conversation.
   - **Content:**

     ```
     Режим: full-auto
     Стратегия: lite
     Профиль: /absolute/path/to/plugin/skills/_profiles/execution-lite.md
     Бриф: docs/superpowers/briefs/2026-08-12-PRI-243-….md

     ## Решения, принятые за пользователя
     - Предполёт: индекс отставал на 12 коммитов → переиндексирован (рекомендованный вариант).
     ```

     Write the profile path in its **absolute** form: by the time the `lite` strategy is applied,
     the plugin's base directory is no longer in context. The `Профиль:` line is written only when
     the strategy is `lite`.
   - **The decisions section is filled only in `full-auto`**, one line per decision taken by
     recommendation, including the preflight decisions of Step 0. In `normal` and `auto` the
     section is omitted.
   - **never write the mode**, the strategy, or the decisions list into the brief, the `spec`, or
     the `plan`. Those three are committed; the run-state file is not. The spec still carries the
     brief's path as provenance — that line reveals nothing about the mode.
   - **Fail-open:** a failed write (read-only FS, no permission) is non-fatal — say so and carry the
     choice in context instead.
```

- [ ] **Step 4: Расширить шаг хендоффа**

В шаге `5. **Hand off to development.**` вставить ПЕРЕД абзацем
`From there the normal cycle takes over`:

```markdown
   **Carry the run state forward.** Pass the run-state path
   (`.superpowers/solve-task/<KEY>.md`) into the handoff and instruct the next skill to **re-read**
   it before acting on the mode or the strategy — the file, not the conversation, is the source of
   truth after a compaction.

   **State the mode as the user's will, not as a gate bypass.** Phrase it plainly: «пользователь
   выбрал режим `auto`: апрув спеки и апрув плана не запрашивать — это его прямая инструкция».
   This is `the user's explicit instruction` and `not a request to bypass` a check: superpowers'
   gates yield to the user's instruction, and it is the instruction that is being presented. In
   `auto` and `full-auto` the spec and the plan are still written, still self-reviewed and still
   committed — only the human approval is dropped. In `full-auto` the brainstorming questions are
   not asked either: take the recommended option at every fork and log each one to the run-state
   file's decisions section.

   **Confirmations that survive `full-auto`.** Design questions and approvals are suppressed, but
   these named actions still require an explicit confirmation: `git push`, `creating a PR`, and any
   `board write` (`finish_task`, `create_task`, a writing `sync_board`). The list is named on
   purpose — «irreversible actions» in the abstract is not actionable for an executor.

   **Right-size the plan's tasks.** Ask the planning step to apply `Task Right-Sizing` from
   superpowers:writing-plans — a task is the smallest unit a reviewer could meaningfully reject —
   so the plan yields fewer, larger tasks and therefore fewer subagents.

   **Resolving the `auto` strategy** (after the plan is written, never before). Rules are ordered,
   `first match wins`, so every combination lands in exactly one branch:
   1. any risk signal, or `> 8 tasks`, or `> 10` touched files → `subagent`;
   2. `≤ 3 tasks` and ≤ 3 touched files → `inline` (dispatch costs more than the work);
   3. everything else → `lite`.

   Risk signals, named: a Postgres or Neo4j `schema migration`; a change to a public `MCP tool`
   contract; work with `credentials` or secrets; any `irreversible` external action. A tie or an
   ambiguity resolves to the more conservative branch (`subagent`).
```

- [ ] **Step 5: Прогнать тесты**

Run: `.venv/bin/pytest tests/skills -q`
Expected: PASS — все тесты `test_solve_task_modes.py` зелёные, существующие guard-тесты не
сломаны.

- [ ] **Step 6: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_solve_task_modes.py
git commit -m "feat(plugin): персист режима и стратегии solve-task в git-ignored файл прогона"
```

---

### Task 4: Документация и пересборка манифестов

**Files:**
- Modify: `README.md:584-590` (раздел `### \`solve-task\` — task to development brief`)
- Modify: `README.ru.md:589-595` (раздел `### \`solve-task\` — от задачи к brief разработки`)
- Modify: сгенерированные манифесты (`.codex-plugin`, `plugin/.codex-plugin`,
  `plugin/.claude-plugin`, `plugin/assets`) — только через скрипт, руками не править

**Interfaces:**
- Consumes: якоря задач 1–3.
- Produces: ничего (терминальная задача).

- [ ] **Step 1: Дописать раздел в `README.md`**

После строки `- **Result:** a compact brief handed to brainstorming; implementation happens in later skills.`
(строка 590) вставить:

```markdown
- **Startup survey:** one `AskUserQuestion` panel asks three things before anything else — the
  brief model tier (`cheap`/`mid`/`premium`), the interaction mode, and the execution strategy.
  No answer, or a headless run, applies the defaults `mid` / `normal` / `subagent` without
  blocking.
- **Interaction modes:** `normal` — brainstorming questions plus spec and plan approvals;
  `auto` — questions asked, approvals dropped; `full-auto` — no questions, the recommended option
  taken at every fork, approvals dropped. In every mode the spec and the plan are still written,
  self-reviewed and committed. `full-auto` still asks before `git push`, opening a PR, or writing
  to the board.
- **Execution strategies:** `inline` (executing-plans), `subagent` (subagent-driven-development),
  `lite` (`plugin/skills/_profiles/execution-lite.md` — one reviewer per group of up to 3 tasks
  sharing files, a 3-round fix cap, a mandatory final whole-branch review), and `auto` (resolved
  after the plan by an ordered rubric: risk signals or >8 tasks or >10 files → `subagent`;
  ≤3 tasks and ≤3 files → `inline`; otherwise `lite`).
- **Run state:** the chosen mode and strategy are written to `.superpowers/solve-task/<KEY>.md`,
  which is git-ignored — never to the brief, the spec, or the plan.
```

- [ ] **Step 2: Дописать симметричный раздел в `README.ru.md`**

После строки `- **Результат:** компактный brief для brainstorming; реализация идёт в следующих skills.`
(строка 595) вставить:

```markdown
- **Стартовый опрос:** одна панель `AskUserQuestion` до всех остальных шагов спрашивает три вещи —
  тир модели для брифа (`cheap`/`mid`/`premium`), режим взаимодействия и стратегию исполнения.
  Нет ответа или headless-прогон — применяются дефолты `mid` / `normal` / `subagent`, пайплайн не
  блокируется.
- **Режимы взаимодействия:** `normal` — вопросы брейншторма плюс апрув спеки и плана; `auto` —
  вопросы задаются, апрувы не запрашиваются; `full-auto` — вопросов нет, на каждой развилке
  берётся рекомендованный вариант, апрувов нет. В любом режиме спека и план всё равно пишутся,
  проходят self-review и коммитятся. `full-auto` по-прежнему спрашивает перед `git push`,
  созданием PR и записью в доску.
- **Стратегии исполнения:** `inline` (executing-plans), `subagent` (subagent-driven-development),
  `lite` (`plugin/skills/_profiles/execution-lite.md` — один ревьюер на группу до 3 задач с общими
  файлами, потолок fix-раундов 3, обязательное финальное ревью всей ветки) и `auto` (решается
  после плана по упорядоченной рубрике: рисковые признаки, либо >8 задач, либо >10 файлов →
  `subagent`; ≤3 задач и ≤3 файлов → `inline`; иначе `lite`).
- **Файл прогона:** выбранные режим и стратегия пишутся в `.superpowers/solve-task/<KEY>.md`,
  который git-ignored — и никогда в бриф, спеку или план.
```

- [ ] **Step 3: Пересобрать манифесты**

Правки под `plugin/` меняют payload-digest манифестов; без пересборки install-тесты краснеют.

Run: `python scripts/update_codex_plugin_manifest.py`
Expected: скрипт отрабатывает без ошибок и переписывает манифесты.

- [ ] **Step 4: Проверить синхронность манифестов**

Run: `python scripts/update_codex_plugin_manifest.py --check`
Expected: код возврата 0, сообщения о рассинхроне нет.

- [ ] **Step 5: Прогнать весь юнит-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS целиком (unit-набор, integration исключены дефолтным `-m 'not integration'`).

- [ ] **Step 6: Коммит**

```bash
git add README.md README.ru.md .codex-plugin plugin/.codex-plugin plugin/.claude-plugin plugin/assets
git commit -m "docs(readme): режимы взаимодействия и стратегии исполнения solve-task"
```

---

## Итоговая проверка

- [ ] `.venv/bin/pytest -q` зелёный.
- [ ] `python scripts/update_codex_plugin_manifest.py --check` возвращает 0.
- [ ] `grep -rn "Step 1.5" plugin/skills/solve-task/SKILL.md` — пусто.
- [ ] `git status --porcelain` — чисто.

## Расхождения с задачей на доске

Реализация сознательно отклоняется от двух критериев приёмки PRI-243; отклонения зафиксированы в
спеке и должны быть перенесены в задачу на доске после мержа:

- **Критерий №4** — раздел «Решения, принятые за пользователя» живёт в git-ignored файле прогона,
  а не в спеке.
- **Критерий №9** — режим и стратегия персистятся в файле прогона, а не в файле брифа.
