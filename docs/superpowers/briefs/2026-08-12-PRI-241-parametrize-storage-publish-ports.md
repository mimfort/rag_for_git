# Brief — PRI-241 Параметризовать хостовые порты Postgres/Neo4j в compose и задавать их через install-wizard
https://ru.yougile.com/team/686c049c8af8/#PRI-241

## Task
- ID-295/PRI-241, статус «Запуск / CI / хуки». Хостовые порты paradedb/neo4j зашиты в `docker-compose.yml` литералами; install-wizard пишет `PG_DSN`/`NEO4J_URI` в `.env`, но не трогает compose → смена порта в мастере тихо ломает подключение.
- Требования (из описания): параметризовать `docker-compose.yml:9,15` по образцу web (`docker-compose.yml:26-30`) через `PARADEDB_PUBLISH_PORT:-5433`, `NEO4J_BOLT_PUBLISH_PORT:-7687`, `NEO4J_HTTP_PUBLISH_PORT:-7474` (контейнерные порты 5432/7687/7474 фиксированы); добавить ключи в wizard-группу «Хранилища» и в `ENV_TEMPLATE`/`.env.example`; дефолт publish-порта выводить из `PG_DSN`/`NEO4J_URI`, при ручном расхождении — предупреждать, не блокировать; не трогать `paradedb-test`/`neo4j-test`; обновить README.md/README.ru.md.
- Критерии приёмки: 1) `docker compose config` без переменных даёт прежние порты; 2) с заданными env compose публикует их и `reviewer check` проходит; 3) wizard с нестандартным портом даёт согласованные `.env`; 4) unit-тест на wizard фиксирует новые поля и вывод дефолта из DSN; 5) тестовый профиль не тронут; 6) README синхронны.
- criteria пусты в сторе — весь текст выше взят из description задачи.

## Related work
- ID-276/PRI-222 (done, PR mimfort/rag_for_git#162) — прямой прецедент того же паттерна: параметризация publish-порта через `${VAR:-default}` в `docker-compose.yml` для сервиса `web`, синхронная правка README.md/README.ru.md, guard-тест `tests/test_infrastructure_policy.py::test_compose_web_service_is_opt_in_with_separate_runtime_ports`, документационный guard в `tests/docs/test_readme_onboarding.py`. Важно: в этом PR `REVIEWER_WEB_PUBLISH_PORT` задаётся только вручную в shell/README-примерах — wizard его НЕ пишет в `.env`; PRI-241 впервые заводит publish-порт под управление install-wizard.
- ID-122/PRI-? «reviewer init — мастер онбординга» (done) — исходная реализация wizard-групп (`WIZARD_GROUPS`, `EnvGroup`/`EnvField`, `render_env`, `ENV_TEMPLATE`) в `reviewer/install.py`; новые поля публикуемых портов встраиваются в ту же структуру.
- ID-169/PRI-? «reviewer init: заполнение новых полей (GitLab, доска, веб-админка) + связка с configure-review» (done) — прецедент добавления новых wizard-полей в существующую группу и синхронизации `ENV_TEMPLATE`.
- (dropped 4 из 8 найденных похожих задач: ID-268 healthcheck CPU — про интервалы проб, не про порты; ID-121 lite-режим SQLite — другая инфраструктура; ID-95 purge orphaned tasks и ID-124 CI-рецепт — не относятся к теме портов/wizard)

## Subsystems
- reviewer (корневой кластер) — `install.py` (WIZARD_GROUPS, EnvField/EnvGroup, render_env, ENV_TEMPLATE) и `update_lifecycle.py` (`sync_compose_file`/`_sync_compose_file_unlocked`, `download_compose`, статусы created/adopted/current/updated/preserved) — оба модуля, которые задача требует трогать, лежат в этом кластере.
- reviewer/config — `settings.py::Settings` держит `pg_dsn`/`neo4j_uri` дефолты и паттерн `.env`-резолва; новые publish-порт переменные не обязаны попадать в Settings (они read только compose/wizard-стороной), но дефолты DSN/URI (`localhost:5433`, `localhost:7687`) должны остаться согласованными источником для вывода дефолта publish-порта.
- tests — `tests/test_infrastructure_policy.py` уже парсит `docker-compose.yml` через `yaml.safe_load` и содержит прецедент теста `test_compose_web_service_is_opt_in_with_separate_runtime_ports`; новый тест для paradedb/neo4j должен идти рядом.
- tests/install — `tests/install/test_install_wizard.py` — целевое место для нового unit-теста (критерий приёмки №4).
- tests/docs — guard-тесты паритета README.md/README.ru.md (см. прецедент PRI-222) — потребуется аналогичная проверка для новых секций про storage publish-порты.

## Relevant code
- `docker-compose.yml:9` — `ports: ["127.0.0.1:5433:5432"]` у `paradedb`; менять на `"127.0.0.1:${PARADEDB_PUBLISH_PORT:-5433}:5432"`.
- `docker-compose.yml:15` — `ports: ["127.0.0.1:7474:7474", "127.0.0.1:7687:7687"]` у `neo4j`; менять на `"127.0.0.1:${NEO4J_HTTP_PUBLISH_PORT:-7474}:7474"` и `"127.0.0.1:${NEO4J_BOLT_PUBLISH_PORT:-7687}:7687"`.
- `docker-compose.yml:26-30` — рабочий образец параметризации у `web`: `ports: ["127.0.0.1:${REVIEWER_WEB_PUBLISH_PORT:-8000}:${REVIEWER_WEB_PORT:-8000}"]`; прямой шаблон для копирования.
- `docker-compose.yml:49,63` — `paradedb-test`/`neo4j-test` порты (`55433`, `17474`/`17687`) — задача явно требует их не трогать.
- `reviewer/install.py:252-270` — wizard-группа `EnvGroup(title="Хранилища (Postgres / Neo4j)", ...)` с полями `PG_DSN`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`; сюда добавляются новые `EnvField` для трёх publish-портов.
- `reviewer/install.py:105-124` — `@dataclass class EnvField` (key/prompt_text/default/secret/required) — контракт, которому должны соответствовать новые поля; `default` статичен, поэтому «дефолт из PG_DSN/NEO4J_URI» потребует либо динамического вычисления при построении `WIZARD_GROUPS`/`prompt_groups`, либо отдельной логики в `init` (cli.py), а не просто нового статичного `EnvField.default`.
- `reviewer/install.py:313-334` — `render_env(values, extra)` — рендерит `.env` по `WIZARD_GROUPS`; новые ключи попадут в вывод автоматически, если добавлены в группу.
- `reviewer/install.py:48-115` — `_ENV_TEMPLATE_BASE` (Python-строка, встроенный шаблон `.env`) со секцией `# --- Postgres (ParadeDB :5433) / Neo4j (:7687) — дефолты docker-compose ---` (строки 71-77); сюда добавить `PARADEDB_PUBLISH_PORT=5433`, `NEO4J_BOLT_PUBLISH_PORT=7687`, `NEO4J_HTTP_PUBLISH_PORT=7474`.
- `reviewer/install.py:296-298` — `_GROUP_HEADERS` — заголовок секции для группы «Хранилища» в рендере `.env` (`# --- Postgres (ParadeDB :5433) / Neo4j (:7687) ---`).
- `.env.example` — по CLAUDE.md должен зеркалить `ENV_TEMPLATE`/settings; требует синхронной правки новыми ключами (файл не процитирован сниппетом, но упомянут в задаче и в докстринге `_ENV_TEMPLATE_BASE:51` как «Полный справочник полей — зеркало .env.example»).
- `reviewer/entrypoints/cli.py:1218-1486` — команда `init`; интерактивная ветка (`values = inst.prompt_groups(...)`, строки 1271-1293) — здесь естественное место для логики «вывести дефолт publish-порта из PG_DSN/NEO4J_URI, предупредить при расхождении, не блокировать».
- `reviewer/update_lifecycle.py:189-220` — `_sync_compose_file_unlocked` — апдейтер сравнивает SHA256 всего файла; ручная правка compose (в т.ч. добавление publish-порт переменных вручную пользователем) переводит файл в `preserved` — это ограничение нужно задокументировать в README согласно пункту 5 задачи.
- `reviewer/update_lifecycle.py:63-73` — `download_compose(...)` тянет канонический `docker-compose.yml` с `COMPOSE_URL` (GitHub raw, main-ветка) — подтверждает, что правка `docker-compose.yml` в репозитории автоматически становится источником для апдейтера у существующих инсталляций.
- `reviewer/entrypoints/cli.py:1516-1552` — `_refresh_update_artifacts` — вызывает `sync_compose_file(download_compose())` и печатает статус (created/adopted/current/updated/preserved); релевантно для понимания, как обновлённый compose доедет до уже установленных пользователей.
- `README.md:797`, `README.ru.md:786` — существующие примеры `REVIEWER_WEB_PORT=... REVIEWER_WEB_PUBLISH_PORT=... docker compose --profile web up -d web` — стилевой образец для новой документации по storage-портам.
- (dropped ~7 из ретрива: сниппеты `Settings` (config/settings.py) и точки входа `mcp_server.py`/`cli.py::serve`/`web/app.py`/`web/serve.py` попали в топ по семантической близости к «портам», но не относятся к paradedb/neo4j publish-портам или install-wizard — тема запроса не про web-порт, а он уже параметризован; не информируют реализацию)

## Test exemplars
- `tests/test_infrastructure_policy.py:350` (`test_compose_web_service_is_opt_in_with_separate_runtime_ports`) — прямой паттерн: парсит `docker-compose.yml` через `yaml.safe_load`, проверяет `ports == ["127.0.0.1:${VAR:-default}:${VAR2:-default2}"]` без Docker/сети. Тот же приём подходит для нового теста на paradedb/neo4j (проверка литерала-шаблона строки, а не рантайм-подстановки — `docker compose config` для рантайм-подстановки нужен отдельно, integration/ручной).
- `tests/test_infrastructure_policy.py:261-283` — существующие ассерты на `paradedb-test`/`neo4j-test` ports (`55433`, `17474`/`17687`) — образец, что для dev-сервисов паradedb/neo4j такого явного теста-литерала сейчас НЕТ (только test-профиль покрыт), значит новый тест для критерия приёмки №1 придётся писать с нуля.
- `tests/install/test_install_wizard.py:62-75` (`test_render_env_contains_wizard_keys`) — паттерн: собрать `values` dict, вызвать `inst.render_env(values, extra={})`, проверить вхождение строк вида `KEY=value`; годится как шаблон unit-теста criteria №4 (новые поля публикуемых портов).
- `tests/install/test_install_wizard.py:187-191` (`test_env_template_contains_all_wizard_keys`) — гарантирует, что каждый ключ `WIZARD_GROUPS` присутствует в `ENV_TEMPLATE`; новые поля обязаны пройти этот тест без правки (или потребуют явного апдейта, если тест жёстче).
- `tests/install/test_install_wizard.py:346-401` (`test_init_prompts_only_selected_vcs_provider`) — паттерн monkeypatch `reviewer.install.prompt_groups`, `click.confirm`, вызов `CliRunner().invoke(cli, ["init", "--scope", "global"])` — пригодится для теста «дефолт publish-порта выводится из PG_DSN/NEO4J_URI при интерактивном прогоне init».
- (dropped 0 отдельно — все найденные тестовые сниппеты по install wizard оказались релевантны; общий cliff обрезал ретрив на 15/71 при скоре 0.42, но непоказанные ниже cliff — низкорелевантные хиты того же файла/кластера, не новая информация)

## Constraints / open questions
- criteria пусты в сторе — все критерии приёмки взяты из раздела описания задачи, не из отдельного поля.
- `EnvField.default` — статичное поле dataclass; «дефолт publish-порта из PG_DSN/NEO4J_URI» не укладывается в статичный default напрямую — нужно решить, где именно парсить порт из DSN/URI: во время построения `WIZARD_GROUPS` (модуль-уровень, no access to `current` values) или в `init`/`prompt_groups` в момент интерактивного прогона (есть доступ к текущим значениям `.env`). Второе больше соответствует «предупреждать, не блокировать» при расхождении.
- Не найдено готовой функции парсинга порта из `PG_DSN`/`NEO4J_URI` — придётся написать (или переиспользовать существующий urlparse, если такой есть в `reviewer/config/settings.py`/`gitutil.py` — не подтверждено ретривом, требует отдельной проверки при реализации).
- `_sync_compose_file_unlocked` инвариант preserved: если пользователь уже вручную правил `docker-compose.yml` (например, добавил кастомный порт руками до этой задачи), апдейтер после релиза PRI-241 обнаружит расхождение хэша и оставит файл в `preserved` — новый шаблон не долетит автоматически; это ограничение явно требуется задокументировать (пункт 5 задачи), а не чинить.
- `.env.example` не был процитирован ни одним сниппетом ретрива (Python-only индекс не покрывает `.env`-файлы) — его текущее содержимое нужно прочитать напрямую перед правкой, не полагаясь на этот бриф.
- README.md/README.ru.md: существующие фрагменты про порты найдены только для web-контейнера (строки ~786-797); секция про paradedb/neo4j publish-порты, вероятно, потребует нового блока рядом с существующим onboarding-текстом про `docker compose up -d` (README.md:37,103) — точные номера строк для вставки не подтверждены, требуют прямого чтения файла при реализации.

Собран на: mid (Sonnet), режим: subagent

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 43 · out 16.3K · cache-write 149.6K · cache-read 1.5M
Всего: 1.6M токенов
