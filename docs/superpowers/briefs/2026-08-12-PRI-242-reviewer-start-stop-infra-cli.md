# Brief — PRI-242 reviewer start / reviewer stop: управление инфраструктурой из CLI
https://ru.yougile.com/team/686c049c8af8/#PRI-242

## Task
- Ключ доски: ID-296 (алиас PRI-242), проект PRI, статус «Движок (reviewer CLI/MCP)».
- `reviewer` синхронизирует docker-compose.yml в `~/.config/rag-reviewer/` (`update_lifecycle.py`), но не умеет его запускать/останавливать — нет команд lifecycle.
- Требования: 1) `reviewer start` — `docker compose -f <config_dir>/docker-compose.yml up -d --wait`, сервисы paradedb+neo4j без test-профиля; 2) `reviewer stop` — остановка без удаления томов, `down -v` запрещён (инвариант CLAUDE.md: dev/test в одном Compose-проекте); 3) путь к compose резолвить через `default_config_dir()`, отсутствие файла — подсказка `reviewer update`, не трейсбек; 4) отсутствие docker/демона → русское сообщение + ненулевой exit code; 5) регистрация в `launcher/metadata.py::COMMAND_PRESENTATION`; 6) `reviewer check` при недоступных Postgres/Neo4j советует `reviewer start`; 7) README.md/README.ru.md + unit-тесты с замоканным subprocess.
- Критерии приёмки — взяты из раздела «## Критерии приёмки» описания (в TaskBrief `criteria=[]`, но заголовок присутствует): 1) start поднимает оба сервиса, идемпотентен; 2) stop сохраняет тома/индекс, без `down -v`; 3) обе команды видны в `--help` и TUI-каталоге не-fallback-описанием; 4) отсутствующий compose-файл/docker — понятное сообщение, exit≠0, без traceback; 5) `check` при недоступных хранилищах советует `reviewer start`; 6) unit-тесты покрывают сборку argv, идемпотентность, оба сценария ошибок; `pytest -q` зелёный; 7) README.md и README.ru.md описывают новые команды.
- Test-профиль (paradedb-test/neo4j-test) вне скоупа; публикуемые порты (`PARADEDB_PUBLISH_PORT` и т.п.) уже настраиваются через ENV/.env — отдельного механизма не нужно.

## Related work
- ID-270 (done) «Глобальный интерактивный launcher reviewer без изменения CLI-контрактов» — источник `reviewer/launcher/` и паттерна `COMMAND_PRESENTATION`, которым п.5 задачи требует зарегистрировать start/stop; смотреть, как там оформлялись `summary`/`details`/`scenarios`/`keywords` для похожих write-команд.
- ID-295 (done) «Параметризовать хостовые порты Postgres/Neo4j в compose и задавать их через install-wizard» — трогала тот же управляемый docker-compose.yml и ENV-порты (`PARADEDB_PUBLISH_PORT`), которые start/stop должны прозрачно подхватывать через окружение/.env.
- ID-268 (done) «Healthcheck тестовых сервисов жжёт CPU вхолостую (docker-compose)» — прецедент правки того же compose-файла (healthcheck), полезно свериться с итоговой структурой сервисов paradedb/neo4j перед добавлением `--wait`.
(dropped 3: ID-122 «reviewer init — мастер онбординга» — другой механизм (интерактивный wizard, не lifecycle-команда); ID-98 «auto-конфигурация allowedTools» — не про инфраструктуру/compose; ID-121 «Zero-infra lite-режим (SQLite)» — альтернативная архитектура без Postgres/Neo4j, не про запуск текущей инфраструктуры)

## Subsystems
- reviewer — `update_lifecycle.py` уже держит `COMPOSE_URL`, `default_config_dir()`, `sync_compose_file()` с атомарной записью и file-lock; это база, на которую ляжет резолв compose-пути для start/stop.
- reviewer/entrypoints — `cli.py` (Click-CLI) — место добавления команд `start`/`stop`; `check` уже диагностирует Postgres/Neo4j и требует доработки подсказкой.
- reviewer/launcher — TUI command palette; `COMMAND_PRESENTATION`/`catalog.py` — обязательная точка регистрации (иначе fallback-презентация в TUI).
- reviewer/config — многослойный резолв Settings/`.env`, включая ENV-порты хранилищ (актуально для передачи `PARADEDB_PUBLISH_PORT` в `docker compose up`).
- reviewer/web — веб-админка наблюдаемости; не затронута.
- reviewer/mcp — сессии PR-ревью; не затронута.
- tests — тестовая инфраструктура/политика unit vs integration; новые тесты CLI лягут в `tests/entrypoints/`.
- scripts — синхронизация Codex-манифеста и smoke-тесты дистрибуции; не затронута.

## Relevant code
- reviewer/update_lifecycle.py:102-104 (`default_config_dir`) — резолв пути к `~/.config/rag-reviewer/docker-compose.yml`, как требует п.3 задачи.
- reviewer/update_lifecycle.py:63-73 (`download_compose`) и :179-186 (`sync_compose_file`) — готовый путь «файла нет → скачать и досинхронизировать», вместо падения трейсбеком.
- reviewer/update_lifecycle.py:76-99 (`run_fresh_artifact_refresh`) — образец subprocess-обёртки с инъекцией `run: Callable = subprocess.run` и типизированным результатом (`RefreshProcessResult`); прямой шаблон для вызова `docker compose up/down` с мокаемостью под unit-тесты (п.6 критериев).
- reviewer/entrypoints/cli.py:747-842 (`check`) — сюда добавляется совет `reviewer start` при недоступных Postgres/Neo4j (п.5/6 задачи); видно существующие ветки диагностики Postgres (769-803) и Neo4j (805-815).
- reviewer/entrypoints/cli.py:1516-1552 (`_refresh_update_artifacts`) — образец fail-soft обработки ошибок с накоплением `errors` и `click.ClickException` на выходе, русские сообщения по каждому статусу.
- reviewer/launcher/models.py:31-38 (`CommandPresentation`) и reviewer/launcher/catalog.py:35-47 (`_build_spec`, читает `COMMAND_PRESENTATION.get(path, ...)`) — контракт, под который писать записи для `start`/`stop`; сам словарь лежит в `reviewer/launcher/metadata.py` (путь не процитирован поиском — открыть Read перед правкой).
- reviewer/install.py:443-468 (`publish_port_warnings`) — уже существующая логика сверки локального URL-порта с publish-портом контейнера; подтверждает, что `PARADEDB_PUBLISH_PORT`/аналог для Neo4j идут через ENV/.env, а не через флаги CLI.
- reviewer/services/risk_paths.py:63-69 (`_is_compose_file`) — распознавание имён compose-файлов; не блокер, но полезно свериться при написании диагностических сообщений про сам файл.
(dropped 0: весь найденный код прямо относится к резолву compose-пути, subprocess-паттерну или точке регистрации команд)

## Test exemplars
- tests/test_update_lifecycle.py:110-136 (`test_run_fresh_artifact_refresh_uses_same_python_environment`) — паттерн инъекции `run` callable (без реального subprocess) и проверки собранного argv списком кортежей `(argv, kwargs)`; прямой образец для теста сборки `docker compose ... up -d --wait` argv.
- tests/entrypoints/test_update_command.py:513-529 и :532-553 (`test_refresh_artifacts_preserves_modified_compose_and_updates_clients`, `test_refresh_artifacts_reports_compose_status`) — паттерн `CliRunner().invoke(cli_mod.cli, [...])` + `monkeypatch.setattr(cli_mod, "<имя>", ...)` для мока `download_compose`/`sync_compose_file`, проверка `result.exit_code` и текста в `result.output` — переиспользовать для тестов идемпотентности start и сценариев ошибок (отсутствующий compose-файл/docker).
(dropped 0)

## Constraints / open questions
- Файл `reviewer/launcher/metadata.py` (сам словарь `COMMAND_PRESENTATION`) не процитирован ни одним поиском — перед правкой открыть Read и свериться со стилем существующих записей (structure `summary/details/effects/scenarios/keywords/special_action`).
- Инвариант CLAUDE.md: `docker compose --profile test down -v` запрещён категорически (общий Compose-проект dev+test) — `reviewer stop` не должен вызывать `down -v` ни при каких условиях; вероятно, безопаснее `docker compose stop` (без снятия сети/volume) либо `down` без `-v`, а не `down -v`.
- `get_task_context` не вернул ни связанных задач, ни PR — реализация с нуля, готового diff-примера lifecycle-команды в графе задач нет.
- Нужно решить архитектурно: заводить ли `--wait`/таймаут как флаг CLI или зашить дефолтом (задача формулирует «по умолчанию … с ожиданием готовности (--wait)» — похоже на встроенный флаг `docker compose up`, не CLI-опцию reviewer).
- `PARADEDB_PUBLISH_PORT` и аналогичные ENV должны без правок долетать до `docker compose up` через окружение процесса (subprocess наследует env по умолчанию) — явно передавать не нужно, но стоит проверить на реальном вызове.

---
Собран на: mid tier (Sonnet), режим: subagent
