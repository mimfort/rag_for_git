# Brief — PRI-207 Watermark синка задач не backfill-ит старые задачи → project (PRI-170) пуст у 94/97, scoped search_tasks почти слепой
url: https://ru.yougile.com/team/686c049c8af8/#PRI-207
_(данные задачи — из стора reviewer после preflight `sync_board`; индекс dev переиндексирован в этой сессии @ `8dd053c`, SCIP)_

## Task
- **Симптом.** `search_tasks(project="PRI")` / `get_task(project="PRI")` видят ~3 из ~97 задач. В `tasks`: `project=''` — 94, `PRI` — 3, `TES` — 1. Это НЕ утечка скоупа (SQL верен), а незаполненная колонка `project`.
- **Корень.** `SyncService._sync_provider` (`sync.py:48`): `if raw.timestamp <= cursor: continue` — старые задачи не доходят до normalize/index. Заполнение `project` (добавлено в путь индексации в PRI-170) никогда не backfill-ится на уже-синканутые задачи. `content_hash` не спасает (в него входит только текст, не `project`).
- **Обобщение.** Любое обогащение, добавленное в индексацию позже первого синка задачи, не доезжает до старых задач; `project` — первый пойманный случай.
- **Кандидаты фикса (выбрать на brainstorming):** 1) force/full-режим `sync_board` (обход watermark → полный re-normalize/index); 2) сброс курсора в `index_meta`; 3) одноразовый backfill `project` прямым UPDATE; 4) отдельный «meta-refresh» путь для ВСЕХ enumerate-нутых задач независимо от watermark.
- **Acceptance:** после фикса `search_tasks(project="PRI")` возвращает десятки PRI-задач (не 3); распределение `project` в `tasks` — ~все валидные задачи имеют непустой `project`, ноль пустых среди задач с валидным кодом.

## Related work
- **PR #92 / `384fce7`** (`finish_task` write-through) — тот же watermark-баг, решён точечно: `provider.fetch_one(key) → normalize → index_task` в обход курсора (`reviewer/mcp/service.py:342-366`). Кандидаты 1/4 обобщают ровно этот паттерн на все задачи; `fetch_one`/`normalize`/`index_task` уже есть.
- **PRI-170** (скоуп задач по проекту) — фича, которую этот баг де-факто отключает для основного корпуса: она добавила `project` в путь индексации (`normalize_* → project_prefix` в `reviewer/tasks/boards/`), но только для changed-задач.
- (dropped 4: ID-205 done-target discovery, ID-203 reviewer-priming, ID-206 blast-radius discipline — та же область task-инфры, но иной механизм; ID-204 «TEST … удалить» — шум.)

## Subsystems
- `reviewer/tasks` — синк/сервис/граф/стор задач; ETL-синк (`SyncService`) с watermark-курсором, батчевый `index_tasks_batch`, дедуп по `content_hash` — ядро правки.
- `reviewer/index` — аналогичный паттерн инкрементальной свежести (watermark/`content_hash`); прецедент дедупа переэмбеда, полезен как ориентир.
- `reviewer/mcp` — сервисный слой MCP; здесь живёт `finish_task` write-through (прецедент обхода watermark).
- `reviewer/entrypoints` — регистрация MCP-тула `sync_board` (сюда тянется новый `force`/`full` параметр).

## Relevant code
- `reviewer/tasks/sync.py:48` — **корень**: watermark-гейт `if raw.timestamp <= cursor: unchanged; continue`. Точка правки для кандидатов 1/4 (обойти skip, но не курсор).
- `reviewer/tasks/sync.py:81-96` — `SyncService.run(board, limit, purge_orphaned, keep_with_prs, board_type, status_field)` — сюда тянется новый параметр force/full до `_sync_provider`.
- `reviewer/tasks/sync.py:65-73` — продвижение курсора (`max_ts > cursor → set_index_meta`); force-проход должен продвигать курсор штатно, не регрессировать (иначе следующий синк перестанет быть инкрементальным).
- `reviewer/tasks/service.py:50-60` — `index_task`: meta-only путь (`store.update_meta(..., project)` @ `:53`) **уже штампует `project` без переэмбеда`; embed-путь `:55-60`. Т.е. project пишется, если задача просто ДОШЛА сюда.
- `reviewer/tasks/service.py:124-138` — `index_batch`: разделение `to_embed`/`meta_only` по `content_hash` (`:138`). При force-проходе почти всё → `meta_only` → штамп project ~бесплатно по Voyage.
- `reviewer/tasks/store.py:160-169` — `update_meta`: `UPDATE tasks SET … project=%s WHERE key=%s` — дешёвый backfill-примитив (кандидаты 3/4), без Voyage.
- `reviewer/tasks/store.py:193-207` — `search` scoped `AND project = %(project)s` (@ `:197/:201/:206`); `store.py:111-124` — `get_task` scoped `AND project = %s` (@`:123`); `store.py:171-180` — `list_keys(project)`. SQL корректен — видит лишь пустую колонку (баг не здесь).
- **Blast radius:** цепочка замкнута — MCP-тул `sync_board` (`reviewer/entrypoints/mcp_server.py`) → `SyncService.run` → `_sync_provider` → `TaskService.index_batch` → `TaskStore`. Новый параметр тянется линейно по этой цепочке.

## Test exemplars
- `tests/tasks/test_sync.py:58-76` — `FakeProvider`/`FakeTaskService`/`FakeMeta`; `test_first_sync_indexes_all_and_advances_cursor`, `test_watermark_skips_unchanged` — паттерн для нового теста «force обходит watermark, но курсор продвигается штатно».
- `tests/tasks/test_service_batch.py:208-215` — `test_index_batch_stamps_project_on_meta_only`: доказывает, что meta-only путь штампует `project` в стор (`meta_updates[-1] == "PRI"`) и граф (`task_projects[0] == "PRI"`) — эталон ассертов «project доехал».

## Constraints / open questions
- **Verify before fix (из задачи):** пишет ли **задеплоенный** reviewer-mcp `project` вообще. Локальный код — да (`index_task` зовёт `update_meta(..., project)` на meta-only, подтверждено тестом), но деплой мог быть старше PRI-170 (0.2.24/PyPI + приёмка были вне сессии — см. память `pri-205-status`). Если деплой старше — сначала передеплой, иначе backfill не поможет.
- **Voyage (3 RPM / 10K TPM):** кандидат 2 (сброс курсора) форсит полный переэмбед ~96 задач → упрётся в лимит. Кандидат 1 с существующим `content_hash`-дедупом гонит почти всё через `meta_only` (без embed) → дёшево. Предпочитать дизайны, переиспользующие дедуп.
- **Обобщаемость:** задача явно хочет фикс, покрывающий «любое обогащение, добавленное позже» (не только `project`) → системные кандидаты 1/4 предпочтительнее одноразового SQL-кандидата 3. Кандидат 3 к тому же требует вывести `project` без re-enumerate доски (напр. из префикса алиаса `PRI-N → PRI`) — открытый вопрос дизайна.
- **Живое подтверждение бага:** scoped `search_tasks(project="PRI")` в этой сессии вернул лишь 5 задач (203–207) — только недавние, выше watermark. Корпус тёплый (96 задач, `changed:0`).
- **brief_token_cost** включён (`.review.yml → solve_task.brief_token_cost: true`) → блок расхода токенов допишется к этому брифу автоматически.

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 48.8K · out 46.6K · cache-write 407.1K · cache-read 2.6M
Всего: 3.1M токенов
