# Brief — PRI-205 Server-side discovery done-цели доски (колонки YouGile + поля/значения YouTrack) для configure-review — без стороннего MCP
url: https://ru.yougile.com/team/686c049c8af8/#PRI-205

_Источник данных задачи: reviewer-store (после preflight `sync_board`). Индекс свеж (drift==0), сводки тёплые._

## Task
- **PRI-205** (alias ID-205, статус «Бэклог»). Продолжение фичи *configurable-done-target* (`finish_task`).
- **Проблема:** done-цель (`done_column` для YouGile; `status_field`+`done_state` для YouTrack) в `.review.yml` сейчас заполняется вручную, подсматривая точные названия в UI. Для YouTrack особенно больно: на клиенте нет YouTrack-MCP, а у сервера нет тула отдать поля/значения. configure-review в YouGile-ветке к тому же опирается на сторонний `mcp__yougile__get_columns`.
- **Цель:** новый server-side reviewer MCP-тул `get_board_targets(board_type, project)` (имя обсуждаемо: `describe_board_targets`) — по типу доски+проекту возвращает кандидатов done-цели, используя REST-креды сервера (env). configure-review показывает pick-list вместо ручного ввода — и для YouGile, и для YouTrack, БЕЗ board-MCP на клиенте (симметрично `sync_board`/`finish_task`).
- **Scope:** (1) provider-методы: YouGile `list_columns(project)` → колонки досок проекта `{title,id,boardId}`; YouTrack `list_status_fields(project)` → поля-состояния/enum + значения бандла `[{field, values:[…]}]` (admin customFields API; фолбэк — агрегировать `value(name)`+`$type` из выборки задач). (2) MCP-тул `get_board_targets` — fail-soft, репо-агностичен, креды НЕ возвращает. (3) configure-review: pick-list вместо ручного ввода `done_column`/`status_field`/`done_state`, ask-фолбэк; убрать клиентский `get_columns`. (4) finish-task step 4: явно называть резолвнутую цель. (5) unit-тесты (мок httpx) + guard-тесты + опц. интеграция. (6) деплой: бамп версии + PyPI → reviewer update → live-приёмка на обеих досках.
- **Acceptance:** configure-review на YouGile-репо БЕЗ клиентского MCP предлагает реальные колонки → выбор `done_column`; на YouTrack-репо предлагает поля статуса + значения → выбор `status_field`/`done_state` (напр. `Stage`/`Готово`); finish-task в подтверждении явно называет цель переноса/статуса.
- **Инварианты:** креды только в env (не в `.review.yml`, не в возврате тула); fail-soft (недоступна доска/права → пустой список → скилл откатывается на вопрос); repo-агностичность сервера (`.review.yml` сервер не парсит); discovery — только ЧТЕНИЕ (перенос/completed по-прежнему через `finish_task` с подтверждением).

## Related work
- **configurable-done-target** — `docs/superpowers/specs/2026-07-02-configurable-done-target-design.md` — прямой предшественник (0.2.23): добавил `done_column`/`status_field`/`done_state` в `finish_task` + `provider.finish`. PRI-205 = discovery-надстройка (читать доступные значения вместо ручного ввода). **Паттерн к переиспользованию:** server-side, креды в env, репо-агностичный тул принимает `board_type`+`project` параметрами, клиент читает `.review.yml` и передаёт.
- **configure-review context-limits** — `docs/superpowers/specs/2026-07-01-configure-review-context-limits.md` — свежий образец расширения configure-review: `count_tasks(project)` best-effort → **фолбэк на вопрос** при отсутствии тула/пустом корпусе. Точный шаблон «pick-list из server-тула ИЛИ ask».
- **PRI-168 configure-review skill** — `docs/superpowers/specs/2026-06-24-pri-168-configure-review-skill.md` — базовая структура скилла (step 5b task_board / done-target — точка правки).
- **youtrack-board-provider** — `docs/superpowers/specs/2026-06-24-youtrack-board-provider.md` — `customFields`/`_FIELDS`/`_state_of`, источник значений для `list_status_fields`.
- **PRI-170 task-board-project-scope** — `docs/superpowers/specs/2026-06-24-pri-170-task-board-project-scope.md` — `project_prefix` скоуп (для `list_columns(project)`, как `iter_raw`).
- (dropped 2: `get_task_context` пуст — у PRI-205 нет связанных задач/PR; `search_tasks` вернул только ID-204 TEST-мусор и PRI-203 grounding — иной механизм, не информируют реализацию.)

## Subsystems
- **reviewer/tasks** — REST-провайдеры досок (yougile/youtrack), нормализация, `finish`. Место для новых discovery-методов провайдера.
- **reviewer/mcp** — сервисный слой MCP (`finish_task`/`sync_board`/`board_config`). Место для метода-делегата `get_board_targets` в `MCPReviewService`.
- **reviewer/entrypoints** — CLI + MCP-сервер (регистрация 33+ тулов через FastMCP). Место `@mcp.tool() get_board_targets`.
- **reviewer/config** — `Settings`: `board_creds`/`configured_board_types` (креды из env, наружу не отдаются через `board_config`).

## Relevant code
- `reviewer/tasks/boards/yougile.py:251` — `_resolve_column_id`: логику резолва колонок доски (GET `/columns/{cur}`→`boardId`; GET `/columns?boardId` match по `title`) вынести/переиспользовать в публичном `list_columns(project)`.
- `reviewer/tasks/boards/yougile.py:139` / `:157-160` — `_get_all("/columns", {"boardId"})` внутри `iter_raw` (обход projects→boards→columns) — готовый паттерн перечисления колонок, скоупить по `project_prefix`.
- `reviewer/tasks/boards/yougile.py:270` — `finish` (использует `_resolve_column_id`, возвращает `column_moved`) — контракт done-колонки, который discovery наполняет.
- `reviewer/tasks/boards/youtrack.py:24` — `_FIELDS` (`customFields(name,value(name))`) и `:43` `_state_of` — источник значений для фолбэк-агрегации `list_status_fields` из выборки задач.
- `reviewer/tasks/boards/youtrack.py:207` / `:233` / `:261` — `finish`: GET `customFields(name,$type,value($type,name))` + `_FIELD_TO_ELEMENT` — паттерн запроса типов/значений полей для discovery.
- `reviewer/tasks/boards/base.py:43` — `TaskBoardProvider` (Protocol): добавить сигнатуры `list_columns`/`list_status_fields` (discovery-методы, доска-специфичные); `:17` `project_prefix` — скоуп по проекту.
- `reviewer/tasks/boards/__init__.py:10` — `make_board_provider(settings, type_, status_field=...)`: фабрика провайдера из `board_creds` (env). **Blast radius:** зовётся в `service.finish_task:342` и `make_board_providers:53` — `get_board_targets` пойдёт тем же путём.
- `reviewer/mcp/service.py:324` `finish_task` / `:299` `sync_board` / `:281` `board_config` — эталон server-side тула (резолв `configured_board_types`, `make_board_provider`, `provider.close()` в finally, fail-soft try/except). Сюда добавить `get_board_targets(board_type, project)`.
- `reviewer/entrypoints/mcp_server.py:120` `finish_task` / `:166` `get_board_config` — паттерн `@mcp.tool()`-регистрации (тонкий делегат в `service`).
- `plugin/skills/configure-review/SKILL.md:125-137` — step 5b done-target: заменить ручной ввод `done_column`/`status_field`/`done_state` на pick-list из нового тула; **`:131-132`** явная ссылка на клиентский `get_columns` — переключить на server-тул.
- `plugin/skills/finish-task/SKILL.md:32-34` — step 4 «Offer + confirm»: явно называть резолвнутую done-цель («перенесу в колонку „Готово" + completed» / «выставлю Stage=Готово»), НЕ регрессить подтверждение перед записью.
- (dropped 0)

## Test exemplars
_(file-level: из дерева `tests/tasks/boards/` + design §8.1; не из retrieval-сниппета → без line-номеров)_
- `tests/tasks/boards/test_yougile_finish.py` — мок httpx для YouGile finish (резолв колонки GET `/columns`) → шаблон теста `list_columns`.
- `tests/tasks/boards/test_youtrack_finish.py` — мок httpx для YouTrack finish (`customFields`) → шаблон теста `list_status_fields` (и фолбэк-агрегации).
- `tests/mcp/test_finish_task.py` — monkeypatch провайдера, проверка проброса параметров в тул → шаблон теста `get_board_targets`.
- `tests/skills/test_configure_review_skill.py` + `tests/skills/test_finish_task_skill.py` — guard-тесты вординга скиллов (pick-list вместо ручного ввода / явная done-цель).

## Constraints / open questions
- **Имя тула не финализировано:** `get_board_targets` vs `describe_board_targets` — решить на brainstorming.
- **YouTrack права токена:** admin `/admin/projects/.../customFields` API может требовать доп. прав → фолбэк на агрегацию `value(name)`+`$type` из выборки задач проекта (`_FIELDS` уже тянет).
- **Неуникальность YouGile-колонок** между досками одного проекта → discovery должен возвращать `boardId` и/или скоупить (в `finish` резолв в пределах доски задачи; здесь единой задачи нет → перечислять доски проекта).
- **Деплой не в этой сессии:** server-тул → бамп `0.2.23`→`0.2.24` + PyPI, затем `reviewer update` + live-приёмка на обеих досках (YouGile PRI + YouTrack TES). Live-acceptance вне сессии (нужен передеплой). См. [[finish-task-branch-status]] — предыдущая фича принята на 0.2.23.
- **Реализация:** subagent-driven, код на **Opus** (по заметке задачи и [[model-code-opus-no-fable]]).
- **Верификация задачи:** [[verify-board-task-not-already-done]] — задача «Бэклог», в git/PR не найдено следов discovery-тула; предшественник (configurable-done-target) смержен, PRI-205 — реальная надстройка над ним, не дубль.

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 49K · out 64.2K · cache-write 477.4K · cache-read 4.1M
Всего: 4.7M токенов
