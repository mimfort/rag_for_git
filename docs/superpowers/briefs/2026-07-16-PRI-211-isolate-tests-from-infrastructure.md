# Brief — PRI-211 Изоляция unit-тестов от реальной инфраструктуры
https://ru.yougile.com/team/686c049c8af8/#PRI-211

## Task
- `PRI-211` — unit-прогон без маркера `integration` не должен обращаться к Postgres/сети; попытка должна быстро падать с понятной ошибкой.
- Добавить CI-гарантию для текущего `pytest -q`, вероятная точка — autouse-фикстура в `tests/conftest.py` с исключением для integration-тестов.
- Integration-тесты должны быть отделены от рабочей БД так, чтобы небрежный `TRUNCATE` не мог удалить base-индекс.
- Убрать глобальные `store.clear()` из `tests/index/test_store_hybrid.py` и заменить их scoped cleanup с обязательным `finally`.
- Критерии приёмки находятся в описании задачи; данные задачи получены из reviewer store после scoped sync доски `PRI`.

## Related work
- `PR #110` (`fix/overlay-gc`) — источник трёх уже исправленных случаев: глобальный `ChunkStore.clear`, массовый `SessionStore.delete_expired(0)` и реальный `SessionStore` в unit-тесте; переиспользовать локальные scoped/fake-фиксы как минимальный baseline.
- (dropped 8: текущая задача-дубликат и ID-182/160/121/99/192/95/162 не задают механизм изоляции unit/integration-инфраструктуры).

## Subsystems
- `tests` — корневые unit-тесты уже преимущественно используют DI/fakes; сюда относится общий pytest guard.
- `tests/integration` — реальные Postgres/ParadeDB-прогоны должны получать отдельный безопасный namespace/DSN.
- `tests/mcp` — содержит и unit-тесты с моками, и явно маркированный реальный `SessionStore`.
- `tests/services` — существующий образец полностью инфраструктурно-независимых unit-тестов на fake stores/graphs.

## Relevant code
- `reviewer/index/store.py:65` — lazy `ConnectionPool` открывает реальное соединение при первом DB-вызове; центральная точка, которую unit network guard должен перехватывать до таймаута.
- `reviewer/index/store.py:98` — `ChunkStore.clear(None)` выполняет глобальный `TRUNCATE chunks RESTART IDENTITY`; blast radius графа — 14 integration-test callers.
- `tests/index/test_store_hybrid.py:11` — файл содержит 10 callers `ChunkStore.clear`, включая глобальный clear в `test_two_repo_isolation` на `:157`; все destructive setup/cleanup должны стать repo-scoped.
- `tests/index/test_migrate_base.py:13` — ещё два integration callers глобального clear; защита тестовой БД должна обезвредить их даже до отдельной чистки этого файла.
- `tests/integration/test_pipeline.py:7` — ещё один integration caller глобального clear; входит в системный blast radius требования «не может уничтожить рабочий base-индекс».
- (dropped 0: все найденные code/graph hits учтены; 14 callers сгруппированы по файлам).

## Test exemplars
- `tests/index/test_status_meta.py:12` — безопасный паттерн: уникальный repo, `store.clear("a/x")` и cleanup в `finally` на `:30`.
- `tests/index/test_summary_store.py:100` — fixture чистит только `repo="test/pri167"` до и после yield и закрывает store.
- `tests/mcp/test_session_store.py:28` — реальный Postgres-тест явно помечен `integration` и удаляет только `(repo, pr)` тестовой сессии.
- (dropped 22: destructive/non-exemplar hits перенесены в Relevant code; 19 хвостовых snippets не материализованы из-за лимита MCP-вывода).

## Constraints / open questions
- Выбрать жёсткую границу integration-инфраструктуры: отдельная БД/схема либо test-only DSN с runtime safety check; одной дисциплины scoped cleanup недостаточно для критерия «даже если тест написан небрежно».
- Unit network guard должен разрешать сеть только тестам с маркером `integration` и давать собственную короткую ошибку до retry/таймаутов `psycopg_pool`.
- Определить, запрещать ли `ChunkStore.clear(None)` на уровне production API или только тестовой конфигурации; у метода сейчас явно задокументирован test-only глобальный режим.
- Lazy diff PR #110 был получен, но MCP cap обрезал конкретные тестовые hunks; brief опирается на три подтверждённых случая из задачи и текущий code graph.

Собран на: gpt-5.6-sol (session model; mid-tier override unavailable), режим: inline
