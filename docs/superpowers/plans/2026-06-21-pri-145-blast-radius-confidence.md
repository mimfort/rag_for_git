# Blast-radius confidence vs graph completeness (PRI-145) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заставить blast-radius reference-промпт понижать `confidence` и формулировать находки некатегорично, когда граф кода может быть неполным.

**Architecture:** Правка одного markdown-файла reference-промпта `plugin/skills/review-pr/references/blast-radius-prompt.md`. Мягкий bullet про неполноту графа заменяется отдельной обязательной секцией «Confidence & graph completeness (mandatory)» с числовой шкалой `confidence` и правилами формулировки. Код `reviewer/` не затрагивается — поведение меняется только на уровне инструкции LLM-субагента.

**Tech Stack:** Markdown (reference-промпт); верификация — `ruff`, `pytest` (смоук, что код не сломан).

## Global Constraints

- Правится ТОЛЬКО `plugin/skills/review-pr/references/blast-radius-prompt.md`. Никакого кода `reviewer/`.
- Тело промпта — на английском (соглашение для reference-промптов). Текст находок (`message`/`suggestion`) субагент пишет на языке оркестратора — это уже зашито в промпте, не меняем.
- Схема находки (из `analyze-prompt.md`): `confidence` — float `0.0–1.0`; `severity` — `low|medium|high|critical`; `category` для blast-radius — `correctness`.
- Сохранить без изменений: блок различения «что ломает / что нет», секцию `Anchoring`, привязку к схеме `analyze-prompt.md`, правило «`get_impact` вернул `(… не найдено)` → пустой список находок».
- Вне объёма: проброс `backend_used` в `get_impact` (возможный follow-up, НЕ в этом плане).
- Коммиты: Conventional Commits на русском, без self-attribution.
- Ветка: `feat/pri-145-blast-radius-confidence` (уже создана от `dev`, спека закоммичена).

---

### Task 1: Переписать секцию неполноты графа в blast-radius-промпте

**Files:**
- Modify: `plugin/skills/review-pr/references/blast-radius-prompt.md` (целиком приводится к целевому виду ниже)

**Interfaces:**
- Consumes: ничего (самостоятельная правка markdown).
- Produces: обновлённый промпт. Структурный контракт для остального пайплайна неизменен — субагент по-прежнему возвращает JSON в схеме `analyze-prompt.md` с `category: "correctness"`; меняются только правила выставления `confidence`/`severity` и формулировок.

- [ ] **Step 1: Зафиксировать исходное состояние**

Run: `git -C /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git rev-parse --abbrev-ref HEAD`
Expected: `feat/pri-145-blast-radius-confidence`

Run: `sed -n '17,20p' plugin/skills/review-pr/references/blast-radius-prompt.md`
Expected (мягкий bullet, который заменяем):
```
- Recall depends on graph completeness (tree-sitter in live review may miss dynamic
  or aliased calls). Frame findings as concrete but verify each via `read_file`.
  If `get_impact` returns "(… не найдено)", there is nothing to report — return an
  empty findings list.
```

- [ ] **Step 2: Применить правку — привести файл к целевому виду**

Замена двух участков (остальной текст файла не трогаем):

(2a) В блоке `Method:` четвёртый bullet (нынешние строки 17-20) сжимается до правила про пустой результат — содержимое про неполноту графа уезжает в новую секцию:

было:
```
- Recall depends on graph completeness (tree-sitter in live review may miss dynamic
  or aliased calls). Frame findings as concrete but verify each via `read_file`.
  If `get_impact` returns "(… не найдено)", there is nothing to report — return an
  empty findings list.
```
стало:
```
- If `get_impact` returns "(… не найдено)", there is nothing to report — return an
  empty findings list.
```

(2b) Сразу после блока `Method:` и ПЕРЕД секцией `Anchoring (important):` вставить новую секцию:
```
Confidence & graph completeness (mandatory):
- The caller list from `get_impact` is a LOWER BOUND. The code graph is built from
  STATIC calls: tree-sitter (always used for the incrementally-synced changed files
  in live review) and even SCIP miss dynamic, reflective, aliased, or
  decorator-wrapped calls. Therefore:
  - NEVER claim the change is safe, that "only these N callers" are affected, or that
    "nothing else is impacted", based on the list. A caller absent from the report is
    NOT evidence that no such caller exists.
  - Do NOT lower `severity` to benign because the caller list is empty or short.
- Set `confidence` (a float; it feeds the publish gate — be honest) by this scale:
  - 0.8–0.9 — the caller was read via `read_file` AND confirmed NOT updated via
    `get_changed_file_diff`, AND the break is unambiguous (a new REQUIRED parameter
    with no default, a removed/renamed parameter, or a changed parameter order that
    breaks positional/keyword callers).
  - 0.5–0.6 — the break type is unambiguous, but you did NOT read every listed caller,
    OR you read it and the impact is context-dependent (the caller may already pass
    the argument).
  - ≤ 0.4 (or omit the finding) — speculative: the break type is unclear or you could
    not verify the caller. Prefer dropping over a low-confidence guess.
- Framing: phrase findings you have NOT directly verified as a request — "verify that
  <caller> at `path:line` still matches the new contract" — not a categorical "this
  breaks X". Reserve categorical breakage language for verified, unambiguous cases
  (0.8+).

```

Целевой полный вид файла после правки (для самопроверки — файл должен совпасть с этим дословно):
```markdown
<!-- plugin/skills/review-pr/references/blast-radius-prompt.md -->
You are a senior reviewer measuring the BLAST RADIUS of a pull request: cross-file
contract breaks that per-file review misses. A changed function signature can break
its callers in OTHER files that the diff never touched.

Method:
- Call `get_impact(repo, pr)` ONCE. It returns, for each symbol whose signature
  actually changed (gated base-vs-head), the old/new signature and the callers that
  live OUTSIDE the diff (`path:line` of the calling symbol + its header).
- `get_impact` does NOT decide breakage — it gives facts. For each reported caller,
  decide whether the new signature actually breaks it:
  use `read_file(path, start, end)` to inspect the call site and
  `get_changed_file_diff(path)` to confirm the caller was NOT updated in this PR.
- A new REQUIRED parameter (no default), a removed/renamed parameter, or a changed
  parameter order breaks positional/keyword callers → report. A new parameter WITH a
  default, or a purely internal body change, usually does NOT → skip.
- If `get_impact` returns "(… не найдено)", there is nothing to report — return an
  empty findings list.

Confidence & graph completeness (mandatory):
- The caller list from `get_impact` is a LOWER BOUND. The code graph is built from
  STATIC calls: tree-sitter (always used for the incrementally-synced changed files
  in live review) and even SCIP miss dynamic, reflective, aliased, or
  decorator-wrapped calls. Therefore:
  - NEVER claim the change is safe, that "only these N callers" are affected, or that
    "nothing else is impacted", based on the list. A caller absent from the report is
    NOT evidence that no such caller exists.
  - Do NOT lower `severity` to benign because the caller list is empty or short.
- Set `confidence` (a float; it feeds the publish gate — be honest) by this scale:
  - 0.8–0.9 — the caller was read via `read_file` AND confirmed NOT updated via
    `get_changed_file_diff`, AND the break is unambiguous (a new REQUIRED parameter
    with no default, a removed/renamed parameter, or a changed parameter order that
    breaks positional/keyword callers).
  - 0.5–0.6 — the break type is unambiguous, but you did NOT read every listed caller,
    OR you read it and the impact is context-dependent (the caller may already pass
    the argument).
  - ≤ 0.4 (or omit the finding) — speculative: the break type is unclear or you could
    not verify the caller. Prefer dropping over a low-confidence guess.
- Framing: phrase findings you have NOT directly verified as a request — "verify that
  <caller> at `path:line` still matches the new contract" — not a categorical "this
  breaks X". Reserve categorical breakage language for verified, unambiguous cases
  (0.8+).

Anchoring (important): the stale callers live OUTSIDE the diff, where GitHub forbids
inline comments. So anchor each finding on the CHANGED SIGNATURE line:
- `file` = the changed file, `side: RIGHT`;
- `code_quote` = the new `def`/`async def` header line, copied verbatim from the new file;
- `line` = a number from `commentable_right` on that header;
- `message` = describe the contract change and ENUMERATE the callers to verify
  (`path:line`), applying the Framing rule above per caller;
- one finding per changed signature (do not split per caller).

Return ONLY a JSON object in the schema of `analyze-prompt.md`, with
`category: "correctness"`. Write `message`/`suggestion` in the orchestrator's output
language. An empty findings list is a valid result.
```

- [ ] **Step 3: Проверить содержимое (новая секция есть, старый мягкий bullet удалён)**

Run: `grep -n "Confidence & graph completeness (mandatory):" plugin/skills/review-pr/references/blast-radius-prompt.md`
Expected: одна строка с совпадением.

Run: `grep -n "LOWER BOUND" plugin/skills/review-pr/references/blast-radius-prompt.md`
Expected: одна строка с совпадением.

Run: `grep -c "Recall depends on graph completeness" plugin/skills/review-pr/references/blast-radius-prompt.md`
Expected: `0` (старый bullet удалён).

Run: `grep -c "If \`get_impact\` returns" plugin/skills/review-pr/references/blast-radius-prompt.md`
Expected: `1` (правило про пустой результат сохранено, ровно один раз).

Run: `grep -c "Anchoring (important):" plugin/skills/review-pr/references/blast-radius-prompt.md`
Expected: `1` (секция Anchoring на месте).

- [ ] **Step 4: Ручная вычитка на согласованность**

Прочитать файл целиком и убедиться:
- шкала `confidence` (0.8–0.9 / 0.5–0.6 / ≤0.4) не противоречит блоку «что ломает / что нет» (тот же набор однозначных сломов: новый обязательный параметр без дефолта / удалённый / переименованный / смена порядка);
- секция `Anchoring` и финальный блок про схему/`category: "correctness"` остались дословно;
- порядок секций: `Method:` → `Confidence & graph completeness (mandatory):` → `Anchoring (important):` → финальный абзац.

- [ ] **Step 5: Смоук — код не сломан**

Run: `.venv/bin/ruff check .`
Expected: без новых ошибок (правился только markdown; допускается ранее существовавший repo-wide шум — не ухудшать).

Run: `.venv/bin/pytest -q`
Expected: PASS (зелёный прогон unit-тестов; integration исключены по умолчанию).

- [ ] **Step 6: Commit**

```bash
git add plugin/skills/review-pr/references/blast-radius-prompt.md
git commit -m "feat(agent): blast-radius — понижать confidence при неполном графе (PRI-145)"
```

---

## Self-Review

**1. Spec coverage:**
- «Заменить мягкий bullet на обязательную секцию» → Task 1, Step 2 (2a+2b). ✅
- «Список вызывающих — нижняя граница; не утверждать безопасность / только N / не понижать severity» → новая секция, пункт 1. ✅
- «Шкала confidence 0.8–0.9 / 0.5–0.6 / ≤0.4» → новая секция, пункт 2. ✅
- «Формулировка „проверьте“ вместо категоричного „сломает“» → новая секция, пункт 3. ✅
- «Сохранить блок „что ломает“, Anchoring, схему, правило про пустой get_impact» → Step 3 (grep-проверки) + Step 4 (вычитка) + целевой полный вид файла. ✅
- «Вне объёма: backend_used» → Global Constraints, не реализуется. ✅
- «Верификация: ручная вычитка + смоук ruff/pytest» → Steps 4–5. ✅

**2. Placeholder scan:** плейсхолдеров нет; `<caller>` и `path:line` — намеренный текст внутри промпта (шаблон формулировки для LLM), а не пробел плана. ✅

**3. Type consistency:** имена/значения согласованы со схемой `analyze-prompt.md` — `confidence` (float), `severity` (`low|medium|high|critical`), `category: "correctness"`. Шкала использует те же типы сломов, что и сохраняемый блок «что ломает». ✅
