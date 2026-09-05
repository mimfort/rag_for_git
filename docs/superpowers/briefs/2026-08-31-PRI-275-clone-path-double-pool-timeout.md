# Brief — PRI-275 preflight платит два таймаута пула вместо одного: `_clone_path` глотает исключение недоступности
https://ru.yougile.com/team/686c049c8af8/#PRI-275

## Task
- На остановленных хранилищах `prepare_task_context` отвечает за 60.04 с — два таймаута пула по 30 с, оба внутри ОДНОЙ секции `preflight`.
- Механика: аргумент `self._clone_path(repo)` вычисляется до `build_status_report`, ждёт свой таймаут и гасит исключение сам («Не удалось прочитать путь к клону…»), флаг `state.down` не взводится; второй таймаут приходит из `get_index_meta_row` и уже доходит до `_safe`.
- Сделать: не глотать в `_clone_path` исключения, проходящие `is_storage_unavailable` (пробрасывать в `_safe` либо взводить флаг на месте); сохранить fail-soft для прочих сбоев чтения `repo_clone`; замерить до/после; покрыть тестом ЧИСЛО заходов в стор (счётчиком, не временем).
- Критерии приёмки: (1) один заход в стор вместо двух; (2) время сокращается вдвое, сравниваясь с пачкой задач (30 с); (3) состав `gaps` и классификация не меняются; (4) число заходов закреплено тестом.
- Источник: приёмка релиза 0.7.0 (PRI-269), дефект Д-4, критерий 1 — `eval/pri269_acceptance_report.md`. Задача в сторе: `criteria=[]`, требования взяты из `description`.

## Related work
- PRI-274 (ID-330, активна) — «PoolTimeout не отделён от OperationalError»: хочет вывести `psycopg_pool.PoolTimeout` из класса «хранилище лежит». Прямой конфликт: на мёртвом сторе исключение — именно PoolTimeout, и фикс, завязанный на `is_storage_unavailable`, после PRI-274 перестанет ловить замеренный случай. Согласовать критерий проброса с ней.
- PRI-268 (ID-322, done) — замыкание секций по недоступности хранилища: `_StorageState` + `_safe`; путь `_clone_path` в её спеку не входил, отсюда дефект.
- PRI-276 (ID-332, done) — раздельные флаги по бэкендам и `graph_error` полем из preflight: объясняет, почему после фикса ожидаемое время — 30 с + отдельный заход `related.linked` в Neo4j, а не ровно 30.00 с.
- PRI-277 (ID-333, done) — `cause_detail` (`auth_failed`/`missing_database`) и `classify_storage_failure`: то, что критерий 3 запрещает сдвинуть.
- PRI-269 (ID-323, done) — приёмка 0.7.0, где дефект замерен; там же формат замера «до/после».
- (dropped 1: PRI-272 (ID-328) «недоступность эмбеддера классифицируется как unknown» — тот же модуль `storage_health`, но другой механизм: Voyage, не стор, на число заходов не влияет.)

## Subsystems
- `reviewer/services` — статус индексов, `build_status_report`, «безопасная обработка недоступных хранилищ»: потребитель аргумента `repo_path`.
- `reviewer/entrypoints` — MCP-сервер и CLI (`status`, `check`): второй потребитель того же статус-отчёта, деградация `graph_nodes=None` штатна.
- `tests/services`, `tests/entrypoints` — где живут инварианты fail-soft-диагностики и status rendering.

## Relevant code
- `reviewer/mcp/service.py:3544` — `_TaskContextDeps._clone_path`: `self._path or self._service._repo_clone_path(repo) or ""` — место правки.
- `reviewer/mcp/service.py:1651` — `_repo_clone_path`: `except Exception → log.warning → None`, здесь исключение и теряется; общий для трёх вызывающих.
- `reviewer/mcp/service.py:3547` — `_TaskContextDeps.preflight`: аргумент `self._clone_path(repo)` исполняется первым, вне `try` внутри `_safe`.
- `reviewer/mcp/task_context.py:118` — `_safe`: единственная точка, взводящая `state.mark` и пишущая gap; фикс обязан довести исключение сюда.
- `reviewer/mcp/task_context.py:53` — `_StorageState`: флаги по бэкендам, взводятся только из `_safe`; вердикт считается лениво по первому сбою.
- `reviewer/mcp/task_context.py:146` — `_absorb_graph_error`: при упавшем preflight (`None`) флаг графа не взводится → `related.linked` делает свой заход в Neo4j; влияет на ожидание «30 с» в критерии 2.
- `reviewer/storage_health.py:58` — `is_storage_unavailable`: критерий «пробрасывать или глотать»; сейчас покрывает PoolTimeout как подкласс `OperationalError` (см. PRI-274).
- `reviewer/index/store.py:209` — `get_repo_clone`: свой `except` только на `UndefinedTable`; PoolTimeout летит наружу — глотание строго выше по стеку.
- `reviewer/index/store.py:67` — `_ensure_pool`: `ConnectionPool` без `timeout=` → дефолт psycopg_pool 30 с; цена одного захода.
- `reviewer/services/status.py:58` — `build_status_report(store, graph, repo, branches, repo_path, …)`: первый заход в стор внутри — `store.get_index_meta_row` (`reviewer/index/store.py:316`).
- `reviewer/mcp/service.py:3633` — `_augment_paths`: второй потребитель `_clone_path`, под собственным `except Exception` (секция `code`) — проброс здесь будет проглочен; решить осознанно.
- `reviewer/mcp/service.py:1692` — `_resolve_policy` (секция `task_board`): третий потребитель `_repo_clone_path`, своего `except` не имеет; его вызывающие fail-soft — например `_resolve_context_limits` (`reviewer/mcp/service.py:1777`). Blast radius строгой правки самого `_repo_clone_path` ограничен этими тремя точками.
- (dropped 12: секция `code` payload'а вернула шум мультизапроса — `boards/yougile.py`, `retrieval/multiquery.py`, `metrics/brief_quality/*`, `graph/scip.py`, `config/committed.py`, `graph/family.py`, `tasks/service.py`, `config/settings.py`, `entrypoints/cli.py::init`, `session_store.py`, `graph/store.py`, `review_service.py` — ни один не участвует в пути preflight → repo_clone.)

## Test exemplars
- `tests/mcp/test_service.py:767` — `test_task_context_deps_code_passes_include_tests_false`: образец конструирования реального `_TaskContextDeps(MagicMock(), None)` — естественная точка для счётчика обращений к `components.store`.
- `tests/mcp/test_prepare_task_context.py:365` — `test_storage_failure_short_circuits_remaining_sections`: `assert deps.calls == ["preflight", "linked"]` — образец теста-счётчика, но уровнем ВЫШЕ дефекта (FakeDeps подменяет весь слой deps, где `repo_clone` не стоит ничего).
- `tests/mcp/test_prepare_task_context.py:8` — `FakeDeps` со списком `calls`: шаблон счётчика для нового теста.
- `tests/mcp/test_local_committed_policy.py:85` — `test_store_failure_does_not_break_policy_resolution` (`get_repo_clone.side_effect = RuntimeError("pg down")`): фиксирует fail-soft на НЕ-storage сбое — гарантия критерия 2 и причина ветвиться по `is_storage_unavailable`, а не по любому `Exception`.
- `tests/mcp/test_prepare_task_context.py:485` — тесты классификации (`auth_failed`, `missing_database`, redacted-отрывок, переиспользование вердикта): то, что критерий 3 запрещает сдвинуть.
- `tests/index/test_repo_clone.py:1` — integration-тесты `get_repo_clone` (`pytestmark = integration`).
- (dropped 0.)

## Constraints / open questions
- Конфликт с PRI-274: если PoolTimeout выйдет из `is_storage_unavailable`, критерий проброса в `_clone_path` должен покрывать его отдельно — иначе замеренный сценарий вернётся к двум таймаутам. Решить порядок задач или общий предикат.
- Где ставить правку: (а) только `_TaskContextDeps._clone_path`, (б) строгий `_repo_clone_path` для всего сервиса (три вызывающих: `_resolve_policy`, brief_quality, `_clone_path`), (в) параметр `strict=`. Проброс предпочтительнее «взвести флаг на месте»: `state` живёт в `task_context.py` и deps его не видит.
- `_augment_paths` (секция `code`) зовёт тот же `_clone_path` под своим `except Exception` — при падении Postgres ПОСЛЕ успешного preflight двойная плата воспроизведётся там; в скоуп задачи это не входит, но решение стоит назвать явно.
- Критерий 2 «сравняется с 30 с» после PRI-276 буквально недостижим: при обоих мёртвых хранилищах `related.linked` платит собственный заход в Neo4j (таймауты 5/10/5). Ожидать ~30 с + единицы секунд и зафиксировать это в замере.
- Замер требует `reviewer stop` (остановленные ParadeDB и Neo4j) — рабочая инфраструктура на время замера ложится; вернуть `reviewer start` после.
- Тест обязан считать ЗАХОДЫ, а не время: на фейках обращение к `repo_clone` бесплатно, что и скрыло дефект. Guard-риск: тест, зелёный по построению (см. `tests/mcp/test_prepare_task_context.py` — FakeDeps не доходит до store) — проверять мутационно.
- Индекс переиндексирован в этом прогоне: `dev` @ `f587a55`, 551 файл, граф SCIP 8114 узлов / 19840 рёбер, drift 0. Сводки подсистем тёплые (40).
- `criteria` в сторе пуст — требования читались из `description`; на доске отдельного поля критериев нет.

Собран на: premium (Opus 5), сборка: inline
