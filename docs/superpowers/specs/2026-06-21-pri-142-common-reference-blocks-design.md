# PRI-142 / ID-142 — общие reference-блоки для промптов ревью

**Дата:** 2026-06-21
**Статус:** дизайн одобрен, готов к плану
**Задача:** [PRI-142](https://ru.yougile.com/team/686c049c8af8/#PRI-142) — «Общие reference-блоки для промптов ревью (findings-schema / anti-hallucination / tools / branch-select)»

## Проблема

Одни и те же блоки инструкций для субагентов ревью **дублируются** по нескольким
скилл-промптам и **дрейфят** при правках → непредсказуемое поведение ревью. Это
**maintainability** (единый источник правды), а **не** экономия рантайм-токенов:
субагент всё равно получает правила инлайном (иначе не сможет следовать схеме).

**Карта дублирования (grep по `plugin/skills/`):**

| Блок | Где дублируется (по факту чтения файлов) |
|---|---|
| **findings JSON-схема** | дословно (с вариацией `category`/`side`/`fix`) в `analyze-prompt.md`, `requirements-prompt.md`, `performance-review/SKILL.md`, `maintainability-review/SKILL.md`. `blast-radius-prompt.md` уже **ссылается** «schema of analyze-prompt.md»; `verify-prompt.md` имеет **свою** схему `verdicts`; `review-pr/SKILL.md` только **упоминает** схему (ссылки) |
| **anti-hallucination / anti-noise** | общее ядро в `analyze`, `requirements`, `performance`, `maintainability`, `blast-radius`, `ask` — но у каждого свои хвосты |
| **tool-usage** | почти везде, но **двумя разными наборами тулов**: PR-сессия (`search_code`/`get_related_symbols`/`find_callers`/`get_definition`/`read_file`/`get_changed_file_diff`/`get_impact`) vs session-less (`search_codebase`/`related_symbols`/`callers`/`definition`) у `ask`/`solve-task` |
| **branch-selection** | реально общий лишь у `ask` и `solve-task` (git branch → `REVIEW_BRANCHES` → `branch`-param). В `review-pr/SKILL.md` `REVIEW_BRANCHES` — про **skip PR**, другой смысл, **не выносим** |

**Что выяснили по коду:**

- **Механизм сборки промптов** — `review-pr/SKILL.md` (шаги 3–5): оркестратор
  **читает reference `.md` и включает verbatim** в промпт субагента (Task tool).
  Markdown без нативного include → склейку делает LLM-оркестратор. Это точка, куда
  встраивается чтение `_common/*.md`.
- **Источник правды для findings-схемы** — `Finding` dataclass (`reviewer/vcs/base.py:30`:
  `category/severity/file/line/side/message/suggestion/confidence/fix_*/code_quote`).
  `_common/findings-schema.md` обязан ему соответствовать (иначе разойдётся с парсингом
  в `reviewer/agent/assemble.py`).
- **Установка скиллов** — `reviewer/install.py`:
  - `SKILL_NAMES` (стр.34-37) — явный кортеж 6 скиллов (`ask` в нём **отсутствует** и
    сейчас);
  - `_unpack_skills` (стр.589) и `_skill_file_hashes` (стр.664) обходят **все**
    подкаталоги `plugin/skills/*` → `_common/` распакуется и захэшируется автоматически,
    но в `SKILL_NAMES` его нет (рассогласование «распаковывается, но не зарегистрирован»).
- **Похожих задач в корпусе нет** (`search_tasks` пуст); графовые/Python-тулы нерелевантны
  — задача про markdown-скилы.

## Решение (обзор)

Создать `plugin/skills/_common/` с 4 reference-файлами (только **общее ядро**) и перевести
скилы из скоупа на **runtime-include**: оркестратор/скилы читают `_common/*.md` и склеивают
в промпт субагента на лету. Скилл-специфичные хвосты (line-grounding, confidence-scale,
What-Not-To-Flag, `verdicts`-схема, конкретные имена тулов) **остаются в скиллах**.

### Принятые решения (брейншторм)

| Развилка | Решение | Почему |
|---|---|---|
| Механизм единого источника | **Runtime-include** (скилы/оркестратор читают `_common/*.md` и склеивают в промпт субагента) | Настоящий single-source: правка `_common` сразу везде, без build-шага. Дистрибутив `_common` уже распаковывается install'ом. Формулировка задачи «включение при сборке субагент-промпта» прямо указывает на рантайм |
| Гранулярность блоков | **Общее ядро в `_common` + скилл-специфичные хвосты остаются в скиллах** | Прямо защищает критерий «поведение субагентов не изменилось»: чужие правила не вливаются в промпт, где их не было. Альтернатива (полный параметризуемый блок, макс DRY) отвергнута — риск дрейфа поведения |
| Скоуп файлов | Файлы из задачи **+ `review-pr/SKILL.md`**; `sync-codebase`/`sync-tasks` **не трогаем** | `review-pr/SKILL.md` — часть review-pipeline и тоже дублирует; у `sync-*` иной session-less набор тулов и мало общего → больше риска, меньше выгоды |
| `verify` | **Вне** findings-схемы — у него своя `verdicts`-схема, остаётся как есть | Это не findings; общий блок ему не подходит |
| `branch-selection` | Общий блок только для `ask` + `solve-task` | В `review-pr/SKILL.md` `REVIEW_BRANCHES` имеет другой смысл (skip PR) |

## Архитектура — `plugin/skills/_common/` (4 файла)

Каждый файл несёт **инвариантную часть**; скилл подставляет свою специфику/контекст.

### 1. `_common/findings-schema.md`

- JSON-каркас `{"findings":[{category,severity,file,line,side,code_quote,message,suggestion,fix,confidence}]}`
  + семантика каждого поля.
- `category` — **плейсхолдер**; скилл указывает своё значение
  (`correctness|security|performance|maintainability|style` для analyze;
  фиксированное `requirements`/`performance`/`maintainability`/`correctness` — для соответствующих).
- Должен **по полям совпадать** с `Finding` (`reviewer/vcs/base.py:30`).
- Включают: `analyze`, `requirements`, `performance`, `maintainability`
  (`blast-radius` — через ссылку на `analyze`; `verify` — **не** включает).

### 2. `_common/anti-hallucination.md`

- Принципы-ядро: проверь тулами **прежде** чем заявлять об отсутствии
  (handler/None-check/validation); галлюцинированное отсутствие хуже пропуска; один
  дефект → один finding; стиль/нейминг — не finding, если не влияет на поведение; не
  выдумывай ради квоты, пустой список — валидный результат; точный `code_quote`.
- Скилл-специфичные хвосты **остаются в скиллах**: line-grounding/`commentable_*`
  (analyze), confidence-scale + graph-completeness (blast-radius), «What Not To Flag»
  (maintainability), grounding-contract Q&A (ask).
- Включают: `analyze`, `requirements`, `performance`, `maintainability`, `blast-radius`, `ask`.

### 3. `_common/tool-usage.md`

- **Общая дисциплина поиска:** делай каждый вызов отвечающим на ОДИН вопрос; не
  просматривай файл целиком; идентичные вызовы кэшируются; останавливайся, когда можешь
  решить; используй тулы ПЕРЕД заявлением о кросс-файловых эффектах.
- **Две справочные таблицы тулов** под подзаголовками: «PR-session» и «session-less».
- Скилл одной строкой указывает свой контекст («use the PR-session tools» /
  «use the session-less tools»). Имена тулов берутся по контексту, лишняя таблица —
  безобидная справка.
- Включают: все промпты из скоупа, где есть обращение к тулам — в т.ч. `verify`
  (PR-session набор), хотя findings-схему он не включает.

### 4. `_common/branch-selection.md`

- `git branch --show-current` → если в `REVIEW_BRANCHES` передать как `branch`; если
  пользователь назвал ветку — её; иначе omit (сервер берёт primary). Тот же `branch` —
  для graph-тулов (`callers`/`related_symbols`/`definition`).
- Включают: `ask`, `solve-task`.

## Механизм включения (runtime-include)

- **`review-pr/SKILL.md`** (оркестратор), шаги 3–5: инструкция «прочитай
  `_common/{findings-schema,anti-hallucination,tool-usage}.md` один раз и включи verbatim
  в промпт субагента вместе с `references/<dim>-prompt.md`».
- **Standalone-скилы** (`performance`, `maintainability`, `ask`, `solve-task`): в их
  SKILL.md — «при сборке промпта субагента / в работе включи соответствующие
  `_common/*.md`».
- **Reference-файлы** (`analyze`, `requirements`, `blast-radius`) и SKILL.md **худеют**:
  вырезанный блок заменяется строкой-маркером
  «(общий блок: `_common/X.md` — включается оркестратором)», чтобы человек видел, что блок
  не потерян и где он теперь.

## Install / staleness — `reviewer/install.py`

- `_unpack_skills` (стр.589) и `_skill_file_hashes` (стр.664) уже обходят все подкаталоги →
  `_common/` распакуется и захэшируется **автоматически**.
- **`SKILL_NAMES` (стр.34-37):** добавить `"_common"` — иначе рассогласование
  «распаковывается/хэшируется, но не зарегистрирован».
- **Проверить трактовку `SKILL_NAMES`:** если где-то он используется как «список
  вызываемых скиллов, у каждого есть `SKILL.md`» (у `_common` его нет), ввести отдельную
  константу `SHARED_DIRS` и учитывать её в установке/штампе вместо засорения `SKILL_NAMES`.
  Решение между «добавить в `SKILL_NAMES`» и «`SHARED_DIRS`» принять на этапе плана по
  фактическому использованию константы.
- `ask` отсутствует в `SKILL_NAMES` и сейчас — **вне скоупа** PRI-142 (только отметить,
  не чинить здесь).

## Тестирование / верификация «поведение не изменилось»

- **Новый guard-тест** (`tests/skills/`): для каждого скилла из скоупа собрать промпт
  «как оркестратор» (reference + включённые `_common/*.md`) и проверить, что итог содержит
  все ключевые правила (по маркерам/фразам), а `_common/findings-schema.md` по полям
  соответствует `Finding` (`reviewer/vcs/base.py:30`).
- **Золотой слепок:** снять снимок собранных промптов скиллов **до** рефакторинга; после —
  сверить смысловую эквивалентность (автоснимок в тесте + ручная диффа в PR-описании).
- **Обновить** существующие guard-тесты установки под `_common`:
  `tests/install/test_skills_stamp.py`, `test_skills_staleness.py`,
  `test_install_skills_cli.py` (а также проверить `tests/skills/test_preflight_guardrail.py`,
  `test_sync_tasks_guardrail.py` — не сломались ли от похудевших скиллов).
- Прогон: `.venv/bin/pytest -q` (unit, integration исключены по умолчанию) + `ruff check .`.

## Скоуп

**В работе:** `review-pr/references/{analyze,requirements,blast-radius}-prompt.md`,
`review-pr/SKILL.md`, `performance-review/SKILL.md`, `maintainability-review/SKILL.md`,
`ask/SKILL.md`, `solve-task/SKILL.md`, `reviewer/install.py`, тесты.
`verify-prompt.md` — затронут только если в нём есть общий tool-usage/anti-halluc хвост
(findings-схему не трогаем).
**Не трогаем:** `sync-codebase`, `sync-tasks`.

## Что НЕ делаем (YAGNI / границы)

- Без build-шага/генератора (выбран runtime-include).
- Без «полного параметризуемого блока» — общее ядро + хвосты в скиллах.
- Не выносим `verdicts`-схему verify и branch-skip-логику review-pr (другой смысл).
- Не чиним отсутствие `ask` в `SKILL_NAMES` (вне скоупа).
- Не трогаем `sync-codebase`/`sync-tasks`.

## Инварианты / зависимости

- **Критерии приёмки:** единый источник у блоков; правка правила меняет все промпты
  разом; guard-тесты контента зелёные; поведение субагентов не изменилось.
- `_common/findings-schema.md` ↔ `Finding` (`reviewer/vcs/base.py:30`) ↔ парсинг в
  `reviewer/agent/assemble.py` — должны оставаться согласованными.
- Runtime-include требует, чтобы install распространял `_common/` всем клиентам (уже
  делает через обход подкаталогов; нужно лишь зарегистрировать в `SKILL_NAMES`/`SHARED_DIRS`).
- Связанные работы (контекстно, прямой связи в графе нет): **PRI-160** (solve-task
  store-first), **PRI-114** (skill staleness/stamp — затрагивает те же install-механизмы).
