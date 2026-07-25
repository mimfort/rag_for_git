# Brief — PRI-215 Сделать интеграцию с досками расширяемой и добавить новые провайдеры с полным функциональным паритетом
https://ru.yougile.com/team/686c049c8af8/#PRI-215

## Task

- Данные задачи получены из reviewer store после `sync_board` (канонический ключ `ID-267`, alias `PRI-215`); `index_task` не вызывался.
- Заменить центральные ветвления/закрытые списки типов на расширяемый реестр провайдеров, конфигураций и credentials.
- Ввести полный общий контракт: sync, markdown-нормализация, ссылки/подзадачи/вложения, single read, create, discovery, finish и write-through reindex.
- Добавить Jira как первого нового провайдера, без изменений generic MCP и `SyncService` при подключении следующих адаптеров.
- Сохранить паритет YouGile/YouTrack, создать общую contract-test suite и убрать `yougile|youtrack` из CLI, MCP, skills, конфигурации и документации.
- Критерии приёмки находятся в описании задачи; enrichment через board MCP не нужен.

## Related work

- ID-265 — PR #124 реализовал server-side `create_task` для YouGile/YouTrack: переиспользовать паттерн полного write lifecycle и нормализации описания.
- ID-205 — PR #92 добавил server-side discovery done-целей: адаптеры должны сохранять board-specific discovery за общим контрактом.
- ID-196 — поддержка вложений в `sync_board` задаёт обязательную часть нормализации для нового провайдера.
- ID-140 — server-side ETL и инкрементальная синхронизация задают границу generic sync-слоя, которую реестр не должен размывать.
- (dropped 15: self-reference либо задачи о purge, таймаутах, навыках и иных механизмах, не задающие реализацию реестра/провайдера)

## Subsystems

- `reviewer/tasks` — провайдеры досок, `TaskBrief`-нормализация, watermark-синк и граф задач; основной слой изменения.
- `reviewer/mcp` — generic операции sync/create/finish/discovery и fail-soft write-through без выдачи credentials.
- `reviewer/config` — settings и разрешение credentials/настроенных типов; расширяемость должна жить здесь, не в центральных `if`.
- `tests/tasks` и `tests/mcp` — моковые провайдеры, инварианты sync и сервисный lifecycle для contract suite.

## Relevant code

- `reviewer/tasks/boards/base.py:43` — `TaskBoardProvider` уже задаёт общий Protocol с `normalize`, `normalize_meta`, `finish`, `fetch_one`, `list_done_targets` и `create`; это точка фиксации полного контракта.
- `reviewer/tasks/boards/__init__.py:10` — `make_board_provider` сейчас жёстко ветвится на YouGile/YouTrack и возвращает `None` для неизвестного типа; заменить на регистрацию адаптеров.
- `reviewer/tasks/boards/__init__.py:49` — `make_board_providers` перебирает `configured_board_types()` для мульти-синка; реестр должен питать этот путь.
- `reviewer/tasks/sync.py:24` — `SyncService` работает со списком провайдеров, отдельными курсорами и `normalize`; новый адаптер не должен требовать его правки.
- `reviewer/tasks/sync.py:100` — выбор `board_type`, статусное поле и агрегация по провайдерам являются generic-поведением, которое надо сохранить.
- `reviewer/mcp/service.py:365` — `finish_task` показывает нужный generic lifecycle: resolve configured type, fail-soft, `fetch_one → normalize → index_task`, закрытие провайдера.
- `reviewer/config/settings.py:100` — board type и credentials сейчас представлены отдельными полями YouGile/YouTrack, хотя type уже допускает `jira`; это центральная точка обобщения без утечки секретов.
- (dropped 0: все точечно retrieved symbols напрямую задают границы реестра, sync, MCP lifecycle или credentials)

## Test exemplars

- `tests/tasks/boards/test_base.py:99` — проверяет единую сигнатуру `create` у обеих реализаций; расширить до contract suite каждого зарегистрированного провайдера, включая Jira.
- `tests/tasks/boards/test_base.py:59` — проверка, что `make_board_providers` собирает все настроенные типы; образец для registry/config discovery.
- `tests/tasks/test_sync.py:131` — два провайдера имеют отдельные cursors, а purge использует объединение ключей; регрессия для подключения Jira.
- `tests/mcp/test_finish_task.py:110` — write-through reindex после board write; полный контракт Jira должен проверять это fail-soft поведение.
- `tests/mcp/test_finish_task.py:69` — при нескольких boards без явного типа возвращается error; сохранить явный выбор и при расширяемом наборе типов.
- (dropped 0: все перечисленные примеры непосредственно покрывают provider contract, multi-provider sync либо generic MCP lifecycle)

## Constraints / open questions

- Нужны точные Jira REST API/base URL, схема credentials, project/status mapping и стратегия нормализации Jira markup/attachments; в store их нет.
- Не определён публичный механизм регистрации (Python entry points, явный registry module или DI); он должен позволять добавлять адаптер без правки generic MCP и `SyncService`.
- Contract suite должна отделять обязательные операции от реально board-specific параметров (`status_field`, column/status target) и проверять API/permission/config failures fail-soft без credentials в ответах.
- Поиск кода частично достиг retrieval cliff; brief опирается на прямые сниппеты центральных символов, а не на непроверенные хвостовые результаты.

Собран на: средний, режим: subagent
