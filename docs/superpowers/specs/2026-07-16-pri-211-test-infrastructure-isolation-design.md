# Дизайн — изоляция тестов от рабочей инфраструктуры

Бриф: `docs/superpowers/briefs/2026-07-16-PRI-211-isolate-tests-from-infrastructure.md`

Задача: [PRI-211](https://ru.yougile.com/team/686c049c8af8/#PRI-211)

## Проблема

Default unit-прогон уже исключает маркер `integration` через
`addopts = "-m 'not integration'"` (`pyproject.toml:76-79`), но само исключение не запрещает
неверно написанному unit-тесту открыть реальное соединение. Такой тест либо висит на таймаутах без
Postgres, либо молча пишет в рабочую БД, если dev-стенд доступен.

Integration-тесты используют тот же `Settings().pg_dsn` и тот же Neo4j endpoint, что и рабочий
процесс. В `tests/index/test_store_hybrid.py` десять тестов вызывают `ChunkStore.clear()` без repo,
что приводит к `TRUNCATE chunks RESTART IDENTITY` (`reviewer/index/store.py:98-105`). Аналогичные
вызовы есть в `tests/index/test_migrate_base.py` и `tests/integration/test_pipeline.py`. Графовые
integration-тесты также вызывают глобальный `GraphStore.clear()`.

Точечный scoped cleanup уменьшает риск, но не создаёт системной гарантии: следующий скопированный
`clear()` снова получит доступ к рабочим данным. Нужны независимые границы для unit- и
integration-прогонов.

## Цели

- Любое исходящее сетевое соединение из Python-процесса unit-теста быстро завершается понятной
  ошибкой, включая loopback.
- C-backed подключения psycopg и Neo4j не обходят unit network guard.
- Integration-тесты подключаются только к отдельным ephemeral ParadeDB и Neo4j test-сервисам.
- Случайно переданный production DSN/URI отклоняется до открытия соединения.
- `ChunkStore` больше не предоставляет no-arg API для глобального `TRUNCATE`.
- Default CI-прогон остаётся зелёным без Postgres/Neo4j и превращает случайную сеть в быстрый fail.
- Деструктивные integration-тесты используют repo-scoped cleanup в `finally` как второй слой защиты.

## Не цели

- Запуск integration-suite в publish workflow: задача требует CI-гарантию для unit-suite, а не
  постоянное содержание двух сервисов в CI.
- Полная изоляция сети произвольных subprocess, запущенных тестом. Для неё нужен отдельный runner с
  network namespace; утверждённая граница покрывает Python sockets и используемые DB drivers.
- Удаление `GraphStore.clear()`: физически отдельный Neo4j test-сервис делает его безопасным, а
  изменение graph API не требуется для закрытия дефекта Postgres.
- Автоматический запуск Docker из pytest: lifecycle инфраструктуры остаётся явным.

## Решение

Используется defense in depth из четырёх независимых слоёв:

1. autouse network policy запрещает сеть unit-тестам;
2. integration policy маршрутизирует DB clients только в явно настроенный test-контур;
3. отдельные ephemeral test-сервисы физически не содержат рабочие данные;
4. scoped cleanup и безопасный `ChunkStore.clear(repo)` ограничивают последствия ошибок внутри
   test-контура.

Production runtime не получает test-настроек и не зависит от pytest. Вся маршрутизация находится в
`tests/conftest.py`, Docker Compose и test-only dependencies.

## Unit network policy

`tests/conftest.py` получает autouse fixture, который проверяет
`request.node.get_closest_marker("integration")`.

Для теста без маркера fixture:

- запрещает `socket.socket.connect`, `socket.socket.connect_ex` и `socket.create_connection` через
  стабильный pytest socket guard;
- перехватывает direct psycopg connect и открытие `psycopg_pool.ConnectionPool` до вызова libpq;
- перехватывает создание Neo4j driver до сетевого handshake;
- выдаёт короткое сообщение: сеть запрещена в unit-тестах; тест с реальной инфраструктурой должен
  иметь маркер `integration` и использовать test profile.

HTTP-клиенты с `httpx.MockTransport`, in-process FastAPI `TestClient`, filesystem и subprocess без
сети продолжают работать. Guard не делает allowlist для localhost: зависимость от локального
dev-сервиса является тем же дефектом, что и зависимость от удалённого сервиса.

`pytest-socket` добавляется в dev dependencies для проверенной socket-level блокировки. Отдельные
driver guards обязательны, потому что libpq и другие C-backed клиенты не обязаны использовать
Python `socket`.

## Integration environment

Docker Compose получает profile `test` с двумя сервисами:

- `paradedb-test`: отдельный host port, отдельные test credentials, БД `reviewer_test`, без
  production named volume;
- `neo4j-test`: отдельные HTTP/Bolt ports, отдельные test credentials, без production named volume.

Сервисы не подключаются к рабочим volumes `paradedb_data` и `neo4j_data`. Пересоздание test-profile
может потерять только тестовые данные.

Переменные test-контура документируются в `.env.example`:

- `TEST_PG_DSN` указывает на `paradedb-test/reviewer_test` и содержит короткий connect timeout;
- `TEST_NEO4J_URI`, `TEST_NEO4J_USER`, `TEST_NEO4J_PASSWORD` указывают на `neo4j-test`.

Autouse integration fixture выполняется до остальных function fixtures и до первого `Settings()`:

1. читает production и test endpoints;
2. валидирует test endpoints без сетевого I/O;
3. подменяет `PG_DSN` и `NEO4J_*` test-значениями через `monkeypatch`;
4. разрешает network policy;
5. оборачивает DB entrypoints allowlist-проверкой: psycopg принимает только настроенный
   `TEST_PG_DSN`, Neo4j — только `TEST_NEO4J_URI` и test credentials.

Import-time конструкции вида `DSN = Settings().pg_dsn` в integration-модулях переносятся в fixtures
или тела тестов. Иначе DSN фиксируется во время collection, до autouse fixture.

## Safety validation

Validation завершается до connect и никогда не откатывается на production endpoint.

Для Postgres обязательны все условия:

- `TEST_PG_DSN` не равен текущему `PG_DSN`;
- имя БД имеет test-суффикс и для встроенного profile равно `reviewer_test`;
- direct `psycopg.connect` и каждый `ConnectionPool` получают тот же endpoint, который прошёл
  validation;
- отсутствующий test-сервис даёт короткую connection error с командой запуска profile, а не skip.

Для Neo4j обязательны все условия:

- `TEST_NEO4J_URI` не равен текущему `NEO4J_URI`;
- Bolt endpoint и credentials совпадают с настроенным test-контуром;
- каждый `GraphDatabase.driver` получает только провалидированные test-значения.

Попытка integration-теста передать рабочий DSN/URI напрямую получает safety error до соединения.
Внешняя HTTP-сеть остаётся доступной integration-тестам, которым нужны реальные provider calls;
ограничение действует на инфраструктурные DB endpoints.

## Безопасный `ChunkStore.clear`

Сигнатура меняется с `clear(repo: str | None = None)` на `clear(repo: str)`. Ветка
`repo is None -> TRUNCATE chunks RESTART IDENTITY` удаляется полностью. Метод всегда выполняет
`DELETE FROM chunks WHERE repo = %s`.

По code graph production callers нет; все 14 callers находятся в integration-тестах. Они должны
передавать явный test repo. Изменение намеренно не сохраняет обратную совместимость: no-arg режим
опасен, документирован как test-only и не нужен внешнему production API.

В `tests/index/test_store_hybrid.py` setup/teardown переносится в fixture:

- перед тестом очищаются только repo, которые тест собирается использовать;
- после теста те же repo очищаются в `finally`, даже если assertion упал;
- прямые SQL assertions добавляют `WHERE repo = %s`, чтобы параллельные/остаточные test rows не
  влияли на результат;
- тест изоляции двух repo регистрирует и очищает оба repo.

Тот же scoped pattern применяется к `test_migrate_base.py` и `test_pipeline.py`. Уникальные test
repo должны иметь явный префикс `test/`, чтобы данные были узнаваемы при ручной диагностике.

## Потоки выполнения

### Unit

1. `pytest -q` применяет существующий `not integration` filter.
2. Autouse fixture включает socket и DB-driver guards.
3. Моки и in-process clients выполняются обычно.
4. Реальный connect немедленно падает с инструкцией про `integration` marker.
5. Ни Postgres, ни Neo4j service для прогона не требуются.

### Integration

1. Разработчик явно поднимает `paradedb-test` и `neo4j-test` через Compose profile.
2. `pytest -q -m integration` выбирает integration-тесты.
3. Fixture валидирует `TEST_*` и подменяет runtime settings.
4. Driver allowlist запрещает любой другой DB endpoint.
5. Тесты работают в ephemeral services и очищают только свои repo в `finally`.
6. Test profile явно останавливается; production services и volumes не затрагиваются.

## Ошибки

- Unit network attempt: немедленная test failure, без retry и ожидания connection pool.
- Test profile не поднят: короткий timeout на test endpoint и команда запуска в сообщении.
- `TEST_*` совпадает с production: configuration failure до connect.
- Test DSN не содержит test database: configuration failure до connect.
- Integration test передал другой DSN/URI: driver allowlist failure до connect.
- Cleanup упал: исходный assertion не маскируется, но teardown failure остаётся видимой; рабочие
  данные всё равно недоступны из-за test endpoint boundary.

## Тесты (TDD, red first)

1. Unit socket connect, включая loopback, падает немедленно с понятным сообщением.
2. Unit `ChunkStore.init_schema()` не открывает pool и падает без timeout.
3. Unit Neo4j driver creation блокируется до handshake.
4. `integration` marker отключает общий deny-network, но DB driver allowlist остаётся активным.
5. Production-like `TEST_PG_DSN` и `TEST_NEO4J_URI` отвергаются без сетевого вызова.
6. Direct psycopg и pool с endpoint, отличным от провалидированного test DSN, отвергаются.
7. Neo4j driver с endpoint/credentials, отличными от test config, отвергается.
8. `ChunkStore.clear(repo)` генерирует только repo-scoped `DELETE`; no-arg вызов невозможен.
9. `test_store_hybrid.py` сохраняет все текущие проверки после перехода на scoped fixture и
   repo-filtered SQL.
10. Integration smoke подтверждает, что `Settings` видит `reviewer_test` и test Neo4j endpoint.
11. Global graph cleanup выполняется только внутри `neo4j-test`.
12. Полный `pytest -q` зелёный при недоступных Postgres/Neo4j и не содержит долгих connection
    timeouts.

## CI и документация

`.github/workflows/publish.yml` продолжает запускать `pytest -q` без service containers. Добавляется
job-level timeout как последний предохранитель от зависаний, но основная гарантия — network policy,
которая превращает connect в немедленную ошибку.

`README.md`, `README.ru.md` и `CLAUDE.md` получают:

- команду unit-прогона без инфраструктуры;
- команды поднятия/остановки test profile и запуска integration-suite;
- список `TEST_*` переменных;
- правило: тест с реальной сетью обязан иметь `integration`, а DB integration-тест обязан работать
  через test endpoints;
- предупреждение, что production Compose profile и test profile используют разные services,
  credentials и volumes.

## Критерии приёмки

- `pytest -q` быстро и зелено завершается при полностью недоступных Postgres и Neo4j.
- Новый или существующий unit-тест с реальным socket/psycopg/Neo4j connect падает сразу и объясняет,
  как классифицировать тест.
- `pytest -m integration` не может открыть production Postgres/Neo4j через проектные DB clients.
- Integration cleanup, включая глобальный `GraphStore.clear`, затрагивает только ephemeral
  test-services.
- В `ChunkStore` нет глобального no-arg clear, а все текущие callers передают repo.
- `tests/index/test_store_hybrid.py` не содержит образца глобального `store.clear()` и очищает данные
  в `finally`.
- Publish CI не поднимает Postgres/Neo4j и не висит на DB connection timeout.
