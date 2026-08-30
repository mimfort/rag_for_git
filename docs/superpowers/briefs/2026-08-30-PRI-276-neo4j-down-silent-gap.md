# Brief — PRI-276 Отказ Neo4j не попадает в gaps и стоит 162 с молчания
url: https://ru.yougile.com/team/686c049c8af8/#PRI-276

## Task
- При живом ParadeDB и остановленном Neo4j `prepare_task_context` отвечает 162.57 с (норма 8.5 с), `gaps` — пустой список, `related.linked` содержит текстовую заглушку `(task graph unavailable)`.
- Дефект того же класса, ради которого делался PRI-268: контекст обеднён, но машиночитаемо неотличим от полного — шаг 0a скилла solve-task ищет `cause: storage_unavailable` и не срабатывает.
- Что сделать: (1) не подменять недоступность графа заглушкой ДО того, как исключение дойдёт до `_safe`; (2) запись в `gaps` с `cause: storage_unavailable` и уместным `remedy`; (3) первая распознанная недоступность графа замыкает остальные обращения к нему; (4) состав потерь не меняется — ровно `related.linked`; (5) тест `tests/mcp/test_prepare_task_context.py:118` бросает настоящее neo4j-исключение вместо `RuntimeError`.
- Критерии приёмки — 5 пунктов один-в-один с «что сделать»; источник: `eval/pri269_acceptance_report.md`, разделы «Критерий 9» и «Д-5».
- `criteria: []` в сторе — требования несёт `description` (штатно для этой доски).

## Related work
- PRI-268 (ID-322) — образец механизма: `gap(cause/remedy)`, `_StorageState.down`, замыкание секций после первой недоступности Postgres. Повторить для графа.
- PRI-277 (ID-333, смержен 0ada185) — `classify_storage_failure` + `cause_detail`; свежий слой, на который обязана лечь правка (не дублировать классификацию).
- PRI-269 (ID-323) — приёмка 0.7.0, откуда дефект Д-5 и замер 162 с.
- ID-331 — «preflight платит два таймаута пула вместо одного: `_clone_path` глотает исключение» — тот же класс дефекта для Postgres; решать отдельно, но правка preflight здесь его касается.
- ID-330 — PoolTimeout выдаётся за недоступность хранилища; ID-328 — эмбеддер классифицируется как `unknown`. Тот же класс, вне скоупа.
(dropped 2: ID-268 healthcheck CPU, ID-332 — сама задача.)

## Subsystems
- `reviewer/mcp` — сервисный слой MCP: `build_task_context`, `_TaskContextDeps`, публичные тулы.
- `reviewer/tasks` — граф задач `:Task` поверх Neo4j, «Neo4j опционален с graceful degrade» — именно этот graceful degrade и глотает сигнал.
- `reviewer/graph` — построение и хранение графа кода, конфиг драйвера Neo4j.
(dropped 5: тестовые кластеры и `reviewer/policy` — контекст, не правка.)

## Relevant code
- `reviewer/tasks/service.py:415-426` — `TaskService.get_task_context`: `except Exception → "(task graph unavailable)"`. **Корень дефекта №1**: исключение до `_safe` не доходит.
- `reviewer/tasks/service.py:428-440` — `count_tasks`: тот же паттерн проглатывания графа (не в пути `prepare_task_context`, но однотипен — решить, трогать ли).
- `reviewer/mcp/service.py:3576-3577` — `_TaskContextDeps.linked` → `self._service.get_task_context(...)`; сюда должно приходить исключение.
- `reviewer/mcp/service.py:506-508` — публичный MCP-тул `get_task_context`: его контракт «строка-нота» закреплён тестом, менять на исключение нельзя без развилки (внутренний метод vs проброс только storage-исключений).
- `reviewer/mcp/task_context.py:107-128` — `_safe`: единственная точка, где `is_storage_unavailable` взводит `state.down` и пишет `_storage_gap`.
- `reviewer/mcp/task_context.py:53-74` — `_StorageState`: **один флаг `down` на ВСЕ хранилища**. Взведённый отказом Neo4j он замкнёт `code`/`test_exemplars`/`similar`/`subsystems` (Postgres), нарушив критерий 4. Ключевая развилка дизайна: замыкание должно стать per-store (graph vs postgres).
- `reviewer/mcp/task_context.py:41-50` — `gap()`: 5-ключевая запись (`section/reason/cause/cause_detail/remedy`), уже готова.
- `reviewer/services/status.py:63-67` — `build_status_report`: `graph.count_nodes` обёрнут `except Exception → graph_nodes = None`. **Второй проглоченный таймаут** и вторая половина 162 с; при этом секция `preflight` терять нельзя (критерий 4) — нужен способ взвести флаг графа, не уронив секцию.
- `reviewer/graph/store.py:5-11` — драйвер `GraphDatabase.driver(...)` создаётся без таймаутов → дефолты (`connection_acquisition_timeout=60 с`, `max_transaction_retry_time=30 с`). Прямой рычаг цены каждого захода; 162 с ≈ два таких захода + базовые 8.5 с.
- `reviewer/graph/store.py:301-306` — `count_nodes` (запрос preflight).
- `reviewer/storage_health.py:54-66` — `is_storage_unavailable` уже покрывает `ServiceUnavailable`/`SessionExpired`; классифицировать нечего добавлять, надо лишь довести исключение.
- `reviewer/storage_health.py:68-72` — `storage_remedy`: `reviewer start` только для loopback-эндпоинтов; `neo4j_uri` уже отдаётся в `_TaskContextDeps.storage_endpoints` (`service.py:3553-3562`).
- `reviewer/storage_health.py:172` — `classify_storage_failure` (класс + `cause_detail` + `remedy`, PRI-277).
(dropped 12: подмешанные ретривом файлы досок/эвала/веба, не относящиеся к пути отказа графа.)

## Test exemplars
- `tests/mcp/test_prepare_task_context.py:118` — `test_neo4j_down_empties_linked_only`: бросает `RuntimeError`, требуется настоящее `neo4j.exceptions.ServiceUnavailable`; нужен и второй тест — «Neo4j лёг → Postgres-секции собраны полностью».
- `tests/test_storage_health.py:25-28` — `test_neo4j_driver_errors_are_storage_unavailable`: готовый образец конструирования neo4j-исключений.
- `tests/services/test_status.py:52-59` — `test_build_status_report_not_indexed_and_neo4j_down`: закрепляет текущий graceful degrade preflight; правка status.py заденет его.
- `tests/tasks/test_service.py:315` — закрепляет строку `(task graph unavailable)`: контракт публичного тула, который правка не должна сломать.
- `tests/tasks/test_service_batch.py:470-482` — `test_refresh_meta_batch_skips_graph_loop_when_store_is_down`: готовый паттерн проверки замыкания при мёртвом сторе.
- `tests/entrypoints/test_cli.py:409-438` — `test_check_fails_on_neo4j_error`: как в проекте моделируют отказ Neo4j в CLI.
(dropped 8: тесты досок/эвала/лаунчера — другой механизм.)

## Constraints / open questions
- `gaps` из preflight-вызова — пустой список; `warnings` содержит только сводку `warm_board` (132 задачи, 0 изменений). Инфраструктура на момент сбора жива.
- **Дрейф индекса: `drift = 25`** (indexed_sha `27b3467`, 25 коммитов позади HEAD, включая весь PRI-277) — ретрив был слеп к свежему `storage_health.py`/`task_context.py`; все цитаты выше сверены прямым чтением файлов, не ретривом.
- **Конфликт критериев 3 и 4:** сокращение ожидания через `state.down` в текущем виде замкнёт и Postgres-секции. Открытый вопрос: делать `_StorageState` per-store (флаг для графа отдельно от флага для Postgres) — и как `_safe` узнаёт, к какому хранилищу относится секция.
- **Открытый вопрос по preflight:** он глотает отказ графа намеренно (`graph_nodes=None` — валидная деградация, секция сохраняется). Чтобы срезать первый таймаут, нужен способ поднять факт недоступности графа из `build_status_report` наружу (например, отдельным полем/ошибкой), не превращая preflight в потерянную секцию.
- **Открытый вопрос по контракту публичного тула:** пробрасывать ли из `TaskService.get_task_context` только storage-исключения (тогда публичный `get_task_context` начнёт падать при мёртвом Neo4j) или завести отдельный внутренний метод для `deps.linked`.
- **Рычаг таймаутов драйвера** (`connection_acquisition_timeout`/`max_transaction_retry_time` в `graph/store.py`) снижает цену независимо от замыкания, но меняет поведение всех потребителей графа — решить, входит ли в скоуп.
- **Способ приёмки критерия 3 (162 с → меньше):** юнит-тестами не доказывается; нужен явный интеграционный замер с реально остановленным `rag-reviewer-neo4j-1`, как в PRI-269.
- Смежные открытые дефекты того же класса — ID-331, ID-330, ID-328 — в этот скоуп не входят; правка не должна их предвосхищать или конфликтовать.
- Существующих артефактов по PRI-276 (brief/spec/plan) не найдено.

Собран на: Opus 5 (сессионная модель), сборка: inline
