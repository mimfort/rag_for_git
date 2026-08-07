# Brief — PRI-224 Добавить skill декомпозиции задачи в нативные подзадачи
https://ru.yougile.com/team/686c049c8af8/#PRI-224

## Task

- Источник: reviewer store после свежего `sync_board`; canonical key `ID-278`, alias `PRI-224`, статус «Бэклог».
- Добавить `decompose-task`: прочитать нормализованного родителя и релевантный код, предложить автономные подзадачи с собственными критериями и показать полный preview.
- До одного явного подтверждения пользователя не выполнять ни одного board-write.
- Добавить server-side batch `create_subtasks(parent_key, subtasks, idempotency_key, ...)`; неподдерживаемый provider должен вернуть `unsupported` до записи.
- Для YouGile создать детей, перечитать родителя и присоединить union старых и новых UUID без дублей; вернуть `created`, `attached`, `unattached`, `warnings` для безопасного повтора/partial failure.
- После записи синхронизировать доску и проверить canonical parent/subtask links в store и task graph; покрыть contract, MCP, provider и skill-тестами.

## Related work

- PRI-213 — переиспользовать из PR #124 server-side lifecycle `create_task`: generic provider resolution, canonical task body, write-through, safe errors и обязательный close.
- PRI-227 — PR #151 уже закрепил имя по basename; новый каталог должен регистрироваться как `rag-reviewer:decompose-task` и пройти cross-client payload/docs guards.
- PRI-140 — следовать server-side ETL модели `sync_board`: credentials и REST остаются внутри MCP, клиент передаёт только generic metadata/options.
- (dropped 5: self-hit PRI-224 и задачи про solve-task hygiene, timeout/batching и subsystem prior не задают механизм native-subtask write.)

## Subsystems

- `reviewer/tasks` — provider contract, registry/runtime, YouGile REST, нормализация links, TaskService и task graph.
- `reviewer/entrypoints` — FastMCP-схема и делегация новой batch-операции.
- `reviewer/config` — server-side credentials и безопасные provider options; секреты не должны попадать в результат/логи.
- `tests/tasks` — contract/provider/service/sync patterns на fake store/graph/HTTP без сети.
- `tests/skills` — статические контракты skill flow, confirmation gate, именование и сборка plugin payload.

## Relevant code

- `reviewer/tasks/boards/base.py:100` — `TaskBoardProvider.create` задаёт существующий write-contract; решить, будет ли `create_subtasks` optional capability или обязательным методом всех providers.
- `reviewer/tasks/boards/registry.py:205` — runtime validation требует каждый member из общего списка; добавление обязательного метода расширит blast radius на все зарегистрированные providers.
- `reviewer/mcp/service.py:630` — `MCPReviewService.create_task` является прямым шаблоном provider resolution, safe error payload, write-through и lifecycle; caller graph: FastMCP wrapper и extensibility test.
- `reviewer/entrypoints/mcp_server.py:151` — существующая MCP-обёртка показывает generic schema/docstring/result для server-side board-write.
- `reviewer/tasks/boards/yougile.py:210` — `_read`/`_write` и `YougileBoard` — место многошагового create/read/merge/update с закрытыми credentials.
- `reviewer/tasks/boards/yougile.py:359` — `normalize` перечитывает каждого ребёнка и знает его canonical code, но передаёт его только внутри title.
- `reviewer/tasks/boards/yougile.py:82` — `normalize_yougile` сейчас пишет UUID в `links[].key`; для canonical child links нужен richer mapping и замена ключа на `ID-N`/alias.
- `reviewer/tasks/service.py:28` — `index_task` сохраняет normalized links в граф через `upsert_links`; это write-through точка проверки parent/child результата.
- `reviewer/tasks/graph.py:41` — `TaskGraph.upsert_links` создаёт `TASK_LINK`; `task_context` затем читает эти связи с project scope.
- `reviewer/tasks/boards/http.py:14` — transport повторяет только read-запросы, поэтому idempotency/resume после неоднозначного write failure должны жить уровнем выше.
- (dropped 11: дубли outer-class hits и нерелевантные install/retrieval/other-provider symbols; SCIP не нашёл implementations для Protocol.)

## Test exemplars

- `tests/skills/test_create_task_skill.py:13` — статический паттерн write + `sync_board`; `:25` проверяет confirmation/no-op до записи.
- `tests/skills/test_skill_names.py:70` — frontmatter name обязан совпасть с каталогом (`decompose-task`).
- `tests/install/test_codex_plugin_payload.py:268` — payload динамически регистрирует каждый каталог с `SKILL.md` и требует README markers.
- `tests/mcp/test_create_task.py:109` — provider failure возвращается error-dict и provider закрывается; `:149` показывает fail-soft post-write reindex.
- `tests/mcp/test_board_provider_extensibility.py:162` — полный generic lifecycle target/create/finish/sync и secret-safe provider options.
- `tests/tasks/boards/test_yougile_create.py:60` — fake REST фиксирует POST `/tasks`; `:79` проверяет обязательный второй GET для canonical key.
- `tests/tasks/boards/fakes/yougile.py:53` — reusable HTTP handler/state для POST, parent GET/PUT, retry и partial-failure сценариев.
- `tests/tasks/boards/test_base.py:99` — signature contract для provider write-методов.
- (dropped 9: дубли create-task fixtures и create-path tests других providers не проверяют native YouGile attachment/merge.)

## Constraints / open questions

- `criteria=[]` в store, но description содержит полноценный heading «Критерии приёмки» и восемь критериев; отдельный board fetch/enrichment не нужен.
- Индекс `dev` свеж (`drift=0`, 5239 chunks), task corpus PRI прогрет, subsystem summaries доступны; existing artifacts для PRI-224 не найдены.
- Текущий `links[].key` для YouGile subtask — UUID, а критерий требует canonical child key; нужно сохранить UUID для board update отдельно от graph identity.
- Идемпотентность не имеет найденного durable-примитива: определить scope/TTL/storage ключа и детерминированное соответствие одного preview к уже созданным детям, включая restart MCP.
- YouGile batch не транзакционен, а write transport намеренно не retry-ит: preflight capability должен предшествовать POST, parent перечитывается непосредственно перед union-PUT, а ambiguous/partial outcomes должны быть resumable без дублей.
- Определить размещение детей (наследовать project/column родителя или использовать create target) и поля preview/subtask input; не угадывать это в skill.
- Решить capability contract: optional provider method/spec capability предпочтительнее обязательных unsupported-stubs во всех остальных providers, но результат `unsupported` должен появляться до первого write.
- После plugin-правки пересобрать Codex manifest и обновить README/AGENTS skill lists, иначе payload guard упадёт.

Собран на: openai/gpt-5.6-sol (session model; per-subagent override unavailable), режим: inline
