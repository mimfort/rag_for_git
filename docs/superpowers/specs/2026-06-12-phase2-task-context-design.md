# Спека Фазы 2: контекст задачи в ревью

Дата: 2026-06-12
Статус: одобрен (брейншторм с пользователем)
Базовый дизайн: `2026-06-12-claude-code-migration-design.md` (раздел «Фаза 2»)

## Цель

Скилл `/rag-reviewer:review-pr` при ревью PR находит ключ связанной задачи, читает
задачу с доски через подключённый к сессии MCP доски и передаёт ревью-сабагентам
контекст: «PR заявляет реализацию задачи X — проверь соответствие требованиям».
Появляется новая категория находок — `requirements`.

**Эталонная доска — Yougile** (через существующий `ichinya/yougile-mcp`). Jira
остаётся вторым документированным плейбуком. Абстракция `TaskProvider` —
конфигурируемая: тип доски + имя MCP-сервера + паттерн ключа.

**Инвариант деградации:** доска не настроена / задача не найдена / MCP недоступен →
ревью работает как в фазе 1, без потери находок и без падения прогона.

## Не-цели (вне scope фазы 2)

- Граф и RAG по задачам (фаза 3): узлы `(:Task)`, рёбра `TASK_LINK`/`IMPLEMENTED_BY`,
  эмбеддинги задач, тулы `index_task`/`search_tasks`/`get_task_context`.
- Скилл `/solve-task` (фаза 4).
- Bulk-синк доски.
- Ревью против нескольких задач одновременно (мульти-таск). Фаза 2 — один primary-ключ.
- Авто-публикация результата ревью обратно в доску.
- Бандлинг Yougile MCP в плагин: board MCP подключает пользователь на стороне сессии;
  плагин его не объявляет и не хранит секреты доски.

## Ключевые решения (зафиксированы с пользователем)

| Вопрос | Решение |
|---|---|
| Граница Python↔скилл | Скилл читает доску (board MCP подключён к сессии); Python (`prepare_review`) детерминированно готовит — извлекает ключи + прокидывает конфиг. Python на доску не ходит. |
| Эталонная доска | Yougile (`ichinya/yougile-mcp`, существующий — свой не пишем). Jira — второй плейбук. |
| Выбор ключа | Строгая прецеденция `аргумент > title > body > branch`; один primary-ключ; прочие найденные — в `others` (упомянуть в сводке, не ревьюить против них). |
| Форма проверки | Whole-diff requirements-измерение (как perf/maintainability): видит все диффы + бриф задачи, оценивает покрытие целостно. |
| Категория `requirements` | Включена по умолчанию; проходит существующий `gate()` без правок. Находки без строки уходят в сводку штатным `assemble`. |
| Деградация | fail-open, recall-safe: любой сбой контекста задачи → пропуск requirements-измерения + пометка в сводке; ревью не падает. |
| Board MCP в плагине | Не бандлим. Пользователь подключает board MCP сам; `.review.yml:task_board.mcp` лишь называет его. |

## Формат ключа задачи Yougile

ID задачи Yougile = префикс проекта + номер, человекочитаемый вид `SAI-515` (именно
такой формат принимает `get_task` в `ichinya/yougile-mcp`). Совпадает с форматом Jira,
поэтому **дефолтный `key_pattern: "[A-Z]+-\\d+"` подходит обеим доскам**. ID стабилен
при переносе задачи между проектами; меняется только при смене префикса в настройках
проекта.

## Конфиг `.review.yml`

Читается из base-ветки (как и вся policy — PR не может ослабить собственное ревью).
Новый необязательный блок:

```yaml
task_board:
  type: yougile          # yougile | jira — выбирает плейбук скилла
  mcp: yougile           # имя подключённого MCP-сервера; тулы зовутся mcp__<mcp>__*
  key_pattern: "[A-Z]+-\\d+"   # опц.; дефолт такой же
```

Нет блока `task_board` → контекст задачи выключен, фаза-1-поведение (тихо).

## Поток данных

```
prepare_review (Python)
  ├─ извлечь кандидаты ключей по key_pattern из title / body / head-branch
  ├─ прецеденция → task_keys = {primary, others[]}
  └─ payload += { task_board: {...}, task_keys: {...} }
        │
        ▼
SKILL (между Prepare и Analyze)
  ├─ если task_board задан и task_keys.primary найден:
  │    ├─ по плейбуку references/task-context-<type>.md позвать board MCP
  │    │    (yougile: mcp__<mcp>__get_task(primary))
  │    └─ собрать board-agnostic TaskBrief
  ├─ whole-diff requirements-сабагент(все диффы + TaskBrief)
  │    → findings[category=requirements]
  └─ деградация: нет конфига / ключа / задачи / MCP → пропуск + пометка в сводке
        │
        ▼
publish_review (Python, без изменений)
  gate → grounding → dedup → assemble → publish → history → cleanup
```

## Изменения: Python

### `reviewer/policy/policy.py`

- `ReviewPolicy` получает поле `task_board: dict | None = None`.
- `from_yaml`/`load` парсят ключ `task_board` (как есть, без валидации полей доски —
  board-специфику знает скилл). Отсутствует → `None`.
- `gate()` и `category_enabled()` не меняются: `requirements` — обычная строка-категория,
  `category_enabled("requirements")` уже возвращает `True` по дефолту (пустой вайтлист).

### Извлечение ключей задачи

Новый чистый модуль `reviewer/services/task_keys.py` (извлечение — часть подготовки PR,
поэтому services-слой), тестируемый изолированно, без сети:

```python
def extract_task_keys(
    pattern: str,
    title: str | None,
    body: str | None,
    branch: str | None,
) -> dict:  # {"primary": str | None, "others": list[str]}
```

- Компилирует `pattern` (невалидный паттерн → `{primary: None, others: []}`, fail-soft + warning).
- Сканирует источники в порядке прецеденции `title → body → branch`.
- `primary` = первый матч в порядке прецеденции.
- `others` = прочие уникальные матчи (без `primary`), порядок появления, дедуп.
- Пустой ввод / нет матчей → `{primary: None, others: []}`.

Примечание: явный аргумент пользователя (верхний приоритет прецеденции) разбирает скилл
из `$ARGUMENTS` — Python видит только title/body/branch. Если пользователь задал ключ
явно, скилл использует его вместо `task_keys.primary`.

**Расширение VCS-слоя под источник `branch`.** Сейчас `PullRequest` (`reviewer/vcs/base.py`)
не несёт имя head-ветки — GitHub-провайдер читает `d["head"]["sha"]`, но не
`d["head"]["ref"]`. Добавляем обратносовместимо:
- `PullRequest.head_ref: str | None = None` (новое поле с дефолтом — старые конструкции
  не ломаются);
- `GitHubProvider.get_pull_request` заполняет `head_ref=d["head"]["ref"]`.

Имя head-ветки прокидывается в `extract_task_keys(branch=prq.head_ref)`.

### `reviewer/mcp/service.py` — `_prepared_payload`

Payload `prepare_review` всегда содержит два новых поля (явный `null`, когда контекст
задачи неактивен — так скилл однозначно различает «выключено» и «не нашли ключ»):

```jsonc
// task_board задан в .review.yml:
"task_board": { "type": "yougile", "mcp": "yougile", "key_pattern": "[A-Z]+-\\d+" },
"task_keys":  { "primary": "SAI-515", "others": ["SAI-517"] }

// task_board НЕ задан:
"task_board": null,
"task_keys":  null
```

`task_keys` вычисляется только при заданном `task_board` (иначе `null`); сам primary
внутри может быть `null`, если матчей нет. Извлечение ключей вызывается в
`ReviewService.prepare` после загрузки policy, на вход — `prq.title`, `prq.body`,
`prq.head_ref` и `task_board.key_pattern` (либо дефолт `[A-Z]+-\d+`). Никаких сетевых
вызовов к доске на стороне Python.

## Изменения: скилл

### `plugin/skills/review-pr/SKILL.md`

Новый шаг между «Prepare» и «Analyze»:

> **Task context (optional).** If `task_board` is present and `task_keys.primary` is
> non-null, follow `references/task-context-<task_board.type>.md` to read the task via
> the connected board MCP and build a `TaskBrief`. On any failure (MCP not connected,
> tool error, task not found) skip the requirements dimension and note the reason in the
> summary — never abort the review.

И новое измерение (рядом с perf/maintainability, шаг «Dimensions»):

> **Requirements (whole-diff).** Only if a `TaskBrief` was built. Dispatch one subagent
> with `references/requirements-prompt.md`, all unit diffs, and the `TaskBrief`. It
> checks, for each requirement / acceptance criterion, whether the diff implements it,
> implements it differently, or contradicts it. Returns findings with
> `category: "requirements"`; `line` is set only when a specific changed line
> contradicts a requirement, otherwise `null`.

### Board-agnostic `TaskBrief`

Скилл строит единый бриф независимо от доски:

```
TaskBrief:
  key:         "SAI-515"
  title:       "<task title>"
  description: "<task description / requirements text>"
  criteria:    ["<acceptance criterion / checklist item>", ...]   # best-effort, может быть []
  status:      "<status>"
  url:         "<link to task>"
  links:       [{type, key, title}, ...]   # связанные задачи; опционально
```

### `references/task-context-yougile.md` (новый — эталон)

Плейбук чтения Yougile:
- Tool: `mcp__<task_board.mcp>__get_task` с `task_keys.primary` (код `SAI-515`).
- Маппинг: `title` ← title; `description` ← description; `criteria[]` ← подзадачи/чеклист
  задачи, если присутствуют (best-effort); `url` ← ссылка на задачу; `status` ← статус.
- `links[]` — опционально (фаза 2 может оставить пустым).
- Доп. тулы по необходимости: `get_task_chat`/`get_task_messages` (контекст обсуждения) —
  опционально, на усмотрение скилла; не обязательны.

### `references/task-context-jira.md` (новый — второй плейбук)

- Tool: Atlassian MCP getJiraIssue по `task_keys.primary`.
- Маппинг: `summary→title`, `description`, поле Acceptance Criteria→`criteria[]`,
  issuelinks→`links[]`.

### `references/requirements-prompt.md` (новый)

Английский промпт whole-diff requirements-измерения. Возвращает ту же findings-схему,
что analyze/perf/maintainability (`category, severity, file, line, code_quote, message,
suggestion, fix, confidence`), с `category: "requirements"`. Правила:
- Оценивать ТОЛЬКО заявленную задачу против фактического диффа.
- Для каждого требования/критерия: реализовано / реализовано иначе / противоречит /
  не реализовано.
- `line` — только при конкретной противоречащей изменённой строке; иначе `null`
  (находка уйдёт в сводку штатно).
- Можно звать reviewer-тулы (`search_code`/`find_callers`/`read_file`) для проверки
  «реализовано ли где-то ещё», прежде чем заявить «не реализовано».
- Recall-safe, anti-noise: не выдумывать требования, которых в задаче нет; пустой список —
  валидный результат.
- Текст находок — на `policy.output_language`.

## Категория `requirements` и гейтинг

- Новая категория, **включена по умолчанию** (шум исключён: находки появляются только
  когда задача реально прочитана и измерение запущено).
- Проходит существующий `ReviewPolicy.gate()` без правок: `severity_threshold`,
  `min_confidence`, `ignore`-пути, вайтлист категорий — всё переиспользуется.
- `line: null` → `assemble_review` штатно уводит находку в сводку (`moved_to_summary`).
  Точечные противоречия конкретной строке диффа — inline. Спец-кейсов в Python не вводим.
- `_finding_from_dict` уже коэрцирует произвольную `category`-строку — менять не нужно.
- История (`review_findings`) и веб-админка пишут/показывают `requirements` как любую
  другую категорию — без изменений схемы.
- Документируем категорию в `.review.example.yml` (или README — план уточнит).

## Деградация (fail-open, recall-safe)

| Ситуация | Поведение |
|---|---|
| `task_board` не задан в `.review.yml` | Контекст задачи выключен; фаза-1-поведение, без пометок. |
| `task_board` задан, ключ не найден (`primary == null`) | requirements-измерение не запускается; в сводке: «ключ задачи в PR не обнаружен». |
| Ключ есть, board MCP не подключён / тул упал / задача не найдена | requirements-измерение не запускается; в сводке: «контекст задачи `SAI-515` недоступен: <причина>; требования не проверены». |
| requirements-сабагент упал / вернул мусор | Прочие измерения и публикация продолжаются (как для любого сабагента в фазе 1). |

Ревью целиком не падает ни в одном из случаев.

## Тестирование

### Unit (Python, фейки, без сети)
- `extract_task_keys`: прецеденция `title>body>branch`; мульти-кандидаты → `primary`+`others`;
  дедуп; нет матчей → пусто; невалидный паттерн → пусто + warning; пустые/`None` источники.
- `GitHubProvider.get_pull_request`: заполняет `head_ref` из `d["head"]["ref"]` (мок httpx);
  обратная совместимость `PullRequest` (дефолт `head_ref=None`).
- `ReviewPolicy`: парсинг `task_board` из `.review.yml` (`from_yaml`/`load`); отсутствие →
  `None`; что `task_board` не ломает существующую загрузку policy.
- `prepare_review` payload: наличие/форма `task_board` и `task_keys`; отсутствие блока →
  поля опущены/`null`; что остальной payload не изменился.
- Гейтинг `requirements`: default-on; отсечение по `severity_threshold`/`min_confidence`;
  выключение через `categories: {requirements: false}` и через env-вайтлист `enabled_only`;
  routing `line:null` → сводка (через `assemble_review`).

### E2E / ручное
- dry-run `/review-pr` на PR со ссылкой на Yougile-задачу при подключённом `yougile` MCP:
  убедиться, что бриф собрался и requirements-находки появились.
- Деградация: тот же PR с отключённым board MCP / без ключа → ревью проходит, в сводке
  корректная пометка, прочие категории не пострадали.

Внешние сервисы (доска) — только в E2E/ручном, по конвенции проекта (unit мокает/фейкает).

## Влияние на существующий код

- `publish_review`, `assemble_review`, `dedup`, grounding, gate-механика, история,
  веб-админка — **без изменений** (категория — строка, `line:null` уже поддержан).
- Меняются:
  - `ReviewPolicy` (+поле `task_board` + парсинг);
  - новый модуль `reviewer/services/task_keys.py` (извлечение ключей);
  - `PullRequest` (+`head_ref`) и `GitHubProvider.get_pull_request` (заполнение `head_ref`);
  - `ReviewService.prepare` (вызов извлечения ключей) и `_prepared_payload` (+2 поля);
  - `SKILL.md` (+шаг task-context +requirements-измерение);
  - 3 новых reference-файла (`task-context-yougile.md`, `task-context-jira.md`,
    `requirements-prompt.md`);
  - `.review.example.yml`/README (документация `task_board` и категории `requirements`).

## Источники

- `ichinya/yougile-mcp` — MCP-сервер Yougile (тулы `get_task`, `get_projects`,
  `get_boards`, `get_task_chat`/`get_task_messages`); конфиг через `YOUGILE_API_KEY`.
- `ra53n/yougile-mcp` — альтернативная реализация.
- YouGile REST API v2; справка «ID задач» (формат `SAI-515`: префикс + номер).
