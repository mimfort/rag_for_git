# Унификация dimension-скилов: вынос общего boilerplate

Задача: **ID-143 / PRI-143** (yougile, колонка «Плагин/агент (скилы)», размер S, приоритет низкий).
URL: https://ru.yougile.com/team/686c049c8af8/#PRI-143

## Контекст и проблема

`plugin/skills/performance-review/SKILL.md` и `plugin/skills/maintainability-review/SKILL.md`
содержат дублирующийся boilerplate. По факту (сверено с файлами) дублируются **две** секции:

- **Scope** — байт-в-байт идентична в обоих скилах (включая маркер
  `<!-- include: _common/tool-usage.md -->` и строку «Use the PR-session tools above.»).
- **Output** — почти идентична; отличается значением `category` (`performance` / `maintainability`)
  и одним maintainability-специфичным абзацем про поле `suggestion`.

Секция **Method различается** (perf: 5 пунктов про perf-sensitivity и `read_file`/`search_code`/
`find_callers`; maint: 6 пунктов про simpler-alternative и `suggestion`) — это содержательное
различие, **не дублирование**. Карточка задачи упоминает «Scope/Method/Output», но Method выносить
не нужно и нельзя.

Дублирование → риск дрейфа при правках. Экономии рантайм-токенов нет — цель чисто
**поддерживаемость/консистентность**.

## Цель и критерии приёмки

- Дублирование секций Scope и общего хвоста Output устранено (единый источник в `_common/`).
- Оба скила работают как раньше: собранные промпты эквивалентны прежним по смыслу и проходят
  guard-тесты.
- Frontmatter `description` (триггеры скилов) **не тронут**.

## Ключевое ограничение

Резолв include-маркеров `<!-- include: _common/X.md -->` **нерекурсивный**:
`tests/skills/test_assembled_prompts.py::assemble` (строки 8–21) подставляет содержимое маркеров
**за один проход** и ассертит, что после подстановки строки `<!-- include:` не осталось.

Следствие: **любой новый `_common`-файл НЕ может сам содержать include-маркеры.** Поэтому выносим
только include-free фрагменты, а вложенные маркеры (`tool-usage`, `findings-schema`) и category-строку
оставляем на верхнем уровне `SKILL.md`, где они резолвятся за один проход.

## Выбранный подход: A — прагматичный вынос include-free частей

Рассмотренные альтернативы (отклонены):
- **B — рекурсивный резолвер** (правка `assemble()` + конвенции оркестратора): меняет контракт
  плагина — избыточно и рискованно для S-задачи.
- **C — параметризация `{category}`**: добавляет механику подстановки (тоже изменение контракта).

Выбран **A**: без изменения контракта, минимум риска, соответствует размеру S/low-priority.

## Изменения

### Новые файлы `_common/` (оба без вложенных include-маркеров)

**`plugin/skills/_common/dimension-scope.md`** — байт-идентичная секция Scope целиком:
заголовок `## Scope`, список scope-режимов (`staged`/`unstaged`/uncommitted/branch-vs-base/commit…),
абзац «Do not pick a scope yourself…», абзац «Inside `/reviewer_review-pr`: … review those.».

**`plugin/skills/_common/dimension-output-tail.md`** — общий include-free хвост Output (идентичные в
обоих скилах строки):
- «Standalone runs may additionally render the findings as a readable list after the JSON.»
- «If a finding cannot be tied to a specific line, use the closest changed line and explain the scope
  in `message`.»
- «If there are no meaningful findings, return `{"findings": []}` and say so.» (слово category убрано,
  чтобы строка стала общей; поведенчески эквивалентно)
- «Write `message` and `suggestion` in the output language given by the orchestrator
  (standalone: the user's language).»

### `plugin/skills/performance-review/SKILL.md`

Frontmatter, Goal, Method, Severity — **без изменений**. Заменяются только Scope и Output:

```
# Performance Review

<!-- include: _common/dimension-scope.md -->

<!-- include: _common/tool-usage.md -->
Use the PR-session tools above.

## Goal … ## Method … ## Severity …   (perf-специфика, как сейчас)

## Output

Return only actionable findings.

Return ONLY the findings JSON used by the review pipeline, with
`"category": "performance"`:

<!-- include: _common/findings-schema.md -->
Set "category" to "performance"; "side" is always "RIGHT".

<!-- include: _common/dimension-output-tail.md -->
```

### `plugin/skills/maintainability-review/SKILL.md`

Аналогично; вся maint-специфика (Repository Context / Simplification Heuristics / What Not To Flag /
Goal / Method / Severity) сохраняется. Output отличается category-строкой и абзацем про `suggestion`,
который ставится **перед** `<!-- include: _common/dimension-output-tail.md -->` (лёгкий безвредный
реордеринг прозы внутри Output):

```
## Output

Return only actionable findings.

Return ONLY the findings JSON used by the review pipeline, with
`"category": "maintainability"`:

<!-- include: _common/findings-schema.md -->
Set "category" to "maintainability"; "side" is always "RIGHT".

The `suggestion` field replaces what in the original Codex format appeared after
`Simplification:` — put the concrete simplifying alternative there.

<!-- include: _common/dimension-output-tail.md -->
```

Все четыре маркера (`dimension-scope`, `tool-usage`, `findings-schema`, `dimension-output-tail`)
находятся на верхнем уровне `SKILL.md` → собираются за один проход, `assemble()` не оставляет
неразрешённых маркеров.

## Остаточное дублирование (осознанный минимум)

Без рекурсии остаются идентичными в обоих скилах:
- `<!-- include: _common/tool-usage.md -->` + «Use the PR-session tools above.» (2 строки —
  обрамляют include-маркер, вынести нельзя);
- «Return only actionable findings.» + каркас строки `"category": "…"` + `<!-- include:
  findings-schema.md -->` + «Set "category" to "…"; "side" is always "RIGHT".» — это либо
  category-специфика (смысл различия скилов), либо невыносимый маркер.

## Тесты

- **`tests/skills/test_assembled_prompts.py`** — без правок, остаётся зелёным:
  `test_performance_assembled_schema_and_goal` (`"category": "performance"`, `"confidence": 0.0`,
  `N+1`) и `test_maintainability_assembled_schema_and_whatnot` (`"confidence": 0.0`,
  `What Not To Flag`) проходят, т.к. эти токены остаются в собранном промпте; `assemble()` за один
  проход не оставляет `<!-- include:`.
- **`tests/skills/test_common_blocks.py`** — правим `test_all_four_common_files_exist_nonempty`:
  расширяем список до 6 файлов (добавляем `dimension-scope.md`, `dimension-output-tail.md`),
  переименовываем в `test_all_common_files_exist_nonempty`.
- **Новый guard** в `tests/skills/test_common_blocks.py`: проверка, что ни один файл
  `plugin/skills/_common/*.md` не содержит подстроки `<!-- include:` — кодифицирует нерекурсивный
  инвариант, чтобы будущая правка не сломала сборку.

## Верификация

```bash
.venv/bin/pytest tests/skills/ -q
.venv/bin/ruff check .
```

Оба должны пройти. Дополнительно — глазами сверить, что собранные промпты обоих скилов
семантически эквивалентны прежним (только category-различия + реордеринг maint-абзаца).

## Вне scope (YAGNI)

- Рекурсивный резолвер include / параметризация category (варианты B/C).
- Изменение каких-либо других скилов или reference-промптов (`review-pr/references/*`).
- Любые рантайм-изменения Python-кода reviewer.

## Связанная работа

- **ID-142** «Общие reference-блоки для промптов ревью» — предшественник, создавший механизм
  `_common/` + include-маркеры. Эта задача расширяет тот же паттерн на dimension-скилы.
