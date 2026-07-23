# Brief — PRI-213 Создание задач на доске из reviewer: MCP-тул create_task для YouGile и YouTrack
https://ru.yougile.com/team/686c049c8af8/#PRI-213

## Task

- Добавить server-side запись «создать задачу» в обе поддерживаемые доски (YouGile, YouTrack) — сейчас reviewer умеет только читать (`sync_board`) и закрывать (`finish_task`).
- Текст задачи должен быть удобным в обе стороны: человек видит нормальную структуру в UI доски, LLM при обратном чтении (`get_task` после синка) получает чистый markdown — без `<br />`, `&gt;`, `<div>`; без эмодзи и «иишного» оформления.
- Структура описания фиксированная: Проблема / Что сделать / Критерии приёмки / Контекст.
- Целевая колонка/статус новой задачи — из `.review.yml` (`task_board`), по образцу `done_column`/`done_state`; сервер репо-агностичен.
- Возвращать ключ + URL созданной задачи.
- Задача взята из стора reviewer (после `sync_board` в preflight), `criteria=[]` — требования инлайн в описании (секция «Критерии приёмки» присутствует → enrichment подзадачами не нужен).

## Related work

- ID-140 — sync_board как server-side ETL (`TaskBoardProvider` + REST-провайдеры + инкрементальность): именно тот слой, куда встраивается запись; разворот инварианта «Python не трогает доску» уже сделан там.
- ID-205 — `get_board_targets`: server-side discovery колонок YouGile / полей YouTrack. Готовый резолвер целевой колонки для создания — переиспользовать, а не писать новый обход projects→boards→columns.
- ID-170 — скоуп синка/выдачи по `task_board.project`: создание тоже должно знать project (для YouGile — выбор доски проекта, для YouTrack — `project: <shortName>` в POST).
- ID-196 — вложения YouGile: там уже разбирается HTML описания (`_file_urls_from_text`, `html.unescape`) — тот же текстовый слой, который меняем.
- ID-160 — store-first чтение одиночной задачи: после создания стоит сделать write-through в стор тем же приёмом, что `finish_task`.
- (dropped 3: ID-95 purge orphaned, ID-114 timeout синка, ID-265 — сама эта задача; другой механизм / self-reference)

## Subsystems

- `reviewer/tasks` — провайдеры досок (YouGile/YouTrack REST), нормализация в TaskBrief, SyncService с watermark-курсорами, fail-soft между слоями. Основное место правки.
- `reviewer/mcp` — `MCPReviewService`: резолв провайдера по типу доски, креды из env, write-through реиндексация. Сюда ложится `create_task`.
- `reviewer/entrypoints` — FastMCP-сервер, регистрация тулов (34 шт.), делегация в сервис.
- `reviewer/config` — `Settings.board_creds` / `configured_board_types`; инвариант: креды никогда не возвращаются клиенту.
- `reviewer/policy` — `ReviewPolicy.task_board` из `.review.yml` (клиентская сторона читает ключи и передаёт в тул).
- `tests/tasks` — фейковые store/embedder/graph, тесты normalize/fetch_one/finish на мокнутом REST.

## Relevant code

- `reviewer/tasks/boards/base.py:43` — `TaskBoardProvider` (Protocol): `iter_raw`/`normalize`/`normalize_meta`/`finish`/`fetch_one`/`list_done_targets`. Сюда добавляется `create(...)`; обе реализации обязаны его закрыть.
- `reviewer/tasks/boards/base.py:24` — `RawTask` + `project_prefix()`: канонический ключ vs проектный код (`ID-N` / `PRI-N`).
- `reviewer/tasks/boards/__init__.py:10` — `make_board_provider(settings, type_, status_field=…)`: фабрика по типу доски. Blast radius (callers): `MCPReviewService.finish_task:365`, `MCPReviewService.get_board_targets:412`, `make_board_providers:49` + 5 тестов в `tests/tasks/boards/test_base.py`.
- `reviewer/mcp/service.py:365` — `finish_task`: **точный шаблон** нового `create_task` — резолв `board_type` из `configured_board_types()`, ошибка-словарь вместо исключения, write-through (`fetch_one` → `normalize` → `index_task`), `provider.close()` в `finally`, fail-soft.
- `reviewer/entrypoints/mcp_server.py:120` — регистрация `finish_task` как MCP-тула: сигнатура 1-в-1 с сервисом, докстрока на английском объясняет, какие ключи приходят из `.review.yml`.
- `reviewer/tasks/boards/yougile.py:309` — `finish`: образец записи в YouGile (GET по ключу → PUT по uuid), идемпотентность по вхождению URL, `html.escape` для пользовательского текста (защита от stored XSS) — тот же приём нужен при создании.
- `reviewer/tasks/boards/yougile.py:63` — `normalize_yougile`: `"description": raw.description` — HTML доски уходит в стор **как есть**. Корень второй половины задачи: чистка/конвертация должна появиться здесь (и симметрично при записи).
- `reviewer/tasks/boards/yougile.py:361` — `list_done_targets`: обход projects→boards→columns со скоупом по `project` и cap 500 — готовая основа резолва «в какую колонку класть новую задачу».
- `reviewer/tasks/boards/yougile.py:290` — `_resolve_column_id`: резолв колонки по title, но **только относительно текущей колонки задачи** — для создания нужен резолв «с нуля» по проекту.
- `reviewer/tasks/boards/yougile.py:256` — `fetch_one`: GET `/tasks/{key}` + резолв title колонки. После создания понадобится, чтобы узнать присвоенный `idTaskProject` (`PRI-N`).
- `reviewer/tasks/boards/youtrack.py:213` — `finish`: правка описания обычным `POST /issues/{key}` + **структурное** обновление кастом-поля (никакого command-DSL). Создание идёт тем же стилем: `POST /issues` c `project`/`summary`/`description`.
- `reviewer/tasks/boards/youtrack.py:100` — `normalize_youtrack`: description кладётся как есть — у YouTrack это уже markdown, конвертация нужна **только** для YouGile (асимметрия провайдеров).
- `reviewer/tasks/boards/youtrack.py:325` — `_admin_status_fields`: резолв id проекта через `/admin/projects?query=` — переиспользовать для `project` в POST /issues.
- `plugin/skills/finish-task/SKILL.md` — образец клиентской стороны: читает ключи `task_board` из `.review.yml`, показывает пользователю, **что именно** будет записано, и пишет только после явного подтверждения.
- (dropped: `reviewer/tasks/sync.py`, `reviewer/tasks/service.py::index_batch` — трогаются только косвенно через write-through; `attachments.py` — вне скоупа)

## Test exemplars

- `tests/tasks/boards/test_yougile_finish.py:19` — паттерн: фейковый `_Client` с таблицей маршрутов GET, запись логируется в `calls`; провайдер конструируется через `YougileBoard.__new__` в обход `httpx.Client`. Прямой шаблон для тестов `create`.
- `tests/tasks/boards/test_yougile_finish.py:75` — тест на экранирование HTML в пользовательском тексте (stored XSS) — обязательный аналог для создания.
- `tests/tasks/boards/test_yougile_finish.py:113` — fail-soft: колонка не найдена → warning, остальная запись проходит.
- `tests/tasks/boards/test_youtrack_finish.py` — аналогичный шаблон для YouTrack (структурное обновление кастом-поля, а не DSL).
- `tests/tasks/boards/test_yougile_normalize.py` / `test_youtrack_normalize.py` — сюда лягут тесты конвертации описания (md↔HTML).
- `tests/tasks/boards/test_base.py:30` — фабрика провайдеров (нет ключа → None, неизвестный тип → None, проброс `status_field`).
- `tests/mcp/test_finish_task.py` — тест сервисного слоя: резолв `board_type`, ошибки-словари, fail-soft write-through.
- `tests/skills/test_finish_task_skill.py` — guard-тест клиентского скилла: понадобится аналог, если появится скилл создания задачи.

## Constraints / open questions

- **Нет markdown/HTML-зависимостей** в `pyproject.toml` (только httpx, pypdf, python-docx, pyyaml). Конвертеры md→HTML и HTML→md придётся писать на stdlib (`html`, `html.parser.HTMLParser`, `re`) под ограниченный набор конструкций — либо явно вводить зависимость и обосновать.
- **Живое подтверждение проблемы:** задача PRI-213 заведена через board-MCP в этой сессии — YouGile переписал текст (`<br>` → `<br />`, HTML-примеры в описании частично съедены санитайзером, в хвост описания протёк непарный `</div>`). То есть проблема воспроизводится на самом акте создания, а не только при чтении.
- **YouGile POST /tasks возвращает только uuid** — проектный ключ (`idTaskProject`, `PRI-N`) приходится дочитывать вторым GET (проверено вручную). Значит `create` = минимум 2 запроса + резолв колонки.
- **Асимметрия досок:** YouTrack хранит markdown нативно, YouGile — HTML. Конвертация нужна односторонне; интерфейс должен принимать markdown и не заставлять YouTrack ничего преобразовывать.
- **Обратная совместимость чтения (критерий 3 задачи):** если `normalize_yougile` начнёт чистить HTML, у всех YouGile-задач изменится текст → изменится `content_hash` → полный переэмбеддинг корпуса задач (Voyage free tier 3 RPM / 10K TPM). Решить в брейншторме: включать в скоуп с разовой миграцией или явно отложить.
- **Открытый вопрос:** откуда берётся целевая колонка/статус создания — новые ключи `.review.yml` (`create_column` / `create_state`) или переиспользование `done_column`/`done_state` с инверсией (класть в первую колонку доски). Дефолт при отсутствии ключа тоже надо определить.
- **Открытый вопрос:** нужен ли клиентский скилл (`/reviewer_create-task`) или тул вызывают существующие скилы (solve-task при формулировке свободным текстом). От этого зависит, где живёт «фиксированная структура описания» — в промпте скилла или в валидации сервера.
- **Открытый вопрос:** критерии приёмки — секцией в описании (как сейчас) или подзадачами доски (`criteria[]` тогда заполняется при чтении). Сейчас `criteria` всегда `[]` у обоих провайдеров.
- Правка чего-либо под `plugin/` или бамп версии требует пересборки codex-манифеста (`update_codex_plugin_manifest.py`), иначе install-тесты краснеют.
- Индекс dev переиндексирован в preflight: drift 0, `bb958d6`, граф SCIP (2973 узла / 11433 ребра). Корпус задач прогрет `sync_board` (102 задачи, PRI).

Собран на: премиум-тир (Opus 4.8), режим: inline

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 112 · out 64.5K · cache-write 324.6K · cache-read 5M
Всего: 5.4M токенов
