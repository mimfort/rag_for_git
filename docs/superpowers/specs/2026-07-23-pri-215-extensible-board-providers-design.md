# Дизайн — расширяемые провайдеры досок и Jira Cloud

Бриф: `docs/superpowers/briefs/2026-07-23-PRI-215-extensible-board-providers.md`

Задача: [PRI-215](https://ru.yougile.com/team/686c049c8af8/#PRI-215)

## Проблема

`TaskBoardProvider` уже описывает почти полный lifecycle доски: sync, нормализацию,
single read, create, discovery и finish (`reviewer/tasks/boards/base.py:43`). Однако
`make_board_provider` и `Settings.board_creds()` ветвятся только по YouGile и YouTrack,
а `configured_board_types()` собирает закрытый tuple этих двух типов
(`reviewer/tasks/boards/__init__.py:10`, `reviewer/config/settings.py:162`).

Board-specific знание протекло и выше:

- `SyncService` отдельно вызывает YouTrack `set_status_field`;
- MCP принимает `status_field`, `done_state` и `done_column`;
- skills сами знают, где колонка, а где поле статуса;
- installer хранит отдельный набор полей для каждого известного типа;
- добавление Jira потребовало бы новых `if` в generic слоях.

Нужна одна явная точка расширения, полный обязательный контракт провайдера и Jira Cloud
как доказательство, что новый тип подключается без изменения generic MCP и `SyncService`.

## Цели

- Ввести декларативный in-repo registry провайдеров без декораторов, import-side-effects и
  внешних Python entry points.
- Формировать доступные и настроенные типы из registry и credential schema, а не из tuple или
  цепочки `if`.
- Сделать полный lifecycle обязательным для каждого поддерживаемого провайдера.
- Перевести YouGile и YouTrack на тот же registry и contract suite, что и Jira.
- Реализовать Jira Cloud REST API v3 с полным функциональным паритетом.
- Сделать получение и проверку credentials удобным для Jira, YouTrack и YouGile.
- Убрать board-specific параметры из нового публичного MCP/config/skills API.
- Сохранить старые env и `.review.yml` поля на один compatibility-релиз.
- Гарантировать fail-soft ошибки без утечки токенов, паролей и auth-заголовков.

## Не цели

- Jira Server/Data Center.
- OAuth 2.0 для Jira в первом релизе; используется Jira Cloud API token.
- Scoped Jira API tokens через gateway `api.atlassian.com/ex/jira/<cloudId>`: первый релиз
  использует API token без scopes и прямой site URL, чтобы сохранить утверждённую схему
  `JIRA_BASE_URL + JIRA_EMAIL + JIRA_API_TOKEN`.
- Внешние устанавливаемые пакеты провайдеров и Python entry points.
- Универсальная low-code система, способная описать произвольную доску без Python-адаптера.
- Удаление legacy config в PRI-215: после compatibility-релиза это делается отдельной задачей.
- Автоматическая миграция пользовательского `.review.yml` с записью в репозиторий.
- Upload новых вложений при `create_task`: контракт нормализует существующие вложения при чтении,
  а текущий canonical task document не принимает бинарные файлы.

## Архитектура

### Явный registry

`reviewer/tasks/boards/registry.py` содержит `BoardProviderRegistry` и immutable
`BoardProviderSpec`. Registry — разрешённая центральная точка регистрации; generic MCP,
`SyncService`, `Settings` и installer не содержат знания о конкретных типах.

Один `BoardProviderSpec` описывает:

- `board_type`: стабильный уникальный ключ;
- `factory`: создание настроенного экземпляра полного `TaskBoardProvider`;
- `credential_fields`: env-имена, legacy aliases, secret-флаг, required/default;
- `setup`: labels, официальные ссылки, подсказки и опциональный acquisition hook;
- `option_fields`: provider-specific runtime options и discovery их допустимых значений;
- `default_api_base`;
- человекочитаемые названия target-ов для installer и skills.

YouGile, YouTrack и Jira экспортируют по одному spec из своих adapter-модулей.
`registry.py` явно регистрирует эти specs. Добавление типа меняет только новый adapter,
его spec, одну строку регистрации, contract fixtures и документацию; generic сервисы не меняются.

Registry отклоняет при старте:

- пустой или повторяющийся `board_type`;
- повторяющиеся env-поля с несовместимой семантикой;
- factory без полного runtime-контракта;
- spec без setup/validation metadata;
- попытку объявить частично поддерживаемый provider.

`configured_board_types()` перебирает зарегистрированные specs в стабильном порядке и включает
только те, чья credential schema полностью настроена.

### Credential source

Registry использует один server-internal `ProviderCredentialSource`. Он читает process env и тот же
resolved `.env`, который использует `Settings`, с приоритетом process env. Наружу возвращается
только факт `configured` и безопасные non-secret metadata.

Для обратной совместимости:

- `YOUGILE_API_KEY` и `YOUGILE_API_BASE` остаются основными YouGile полями;
- legacy `TASK_BOARD_API_KEY` и `TASK_BOARD_API_BASE` остаются fallback для YouGile;
- `YOUTRACK_TOKEN` и `YOUTRACK_BASE_URL` сохраняются;
- новые Jira поля: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`.

`board_config()` и ошибки MCP никогда не включают значения credential fields.

### Полный provider contract

`TaskBoardProvider` становится полным обязательным протоколом:

- `validate_connection()` — проверить identity и минимально нужный доступ;
- `iter_raw(board, limit)` — полная пагинированная выдача для watermark и purge;
- `normalize(raw)` — полный `TaskBrief`, Markdown, ссылки, подзадачи и вложения;
- `normalize_meta(raw)` — дешёвые метаданные без I/O;
- `fetch_one(key)` — single read для store-first fallback и write-through;
- `list_targets(project)` — нормализованный discovery целей и provider options;
- `create(doc_md, title, target, project)` — создание с best-effort target;
- `finish(key, pr_url, note, mark_done, target)` — идемпотентный PR-link и done target;
- `close()` — освобождение транспорта.

Провайдер создаётся с immutable validated options. `SyncService` больше не меняет singleton через
YouTrack-specific `set_status_field`.

Discovery возвращает общую форму:

```json
{
  "targets": [
    {"id": "10001", "label": "Done", "purposes": ["create", "done"]}
  ],
  "options": [
    {"key": "issue_type", "label": "Issue type", "required_for": ["create"],
     "choices": [{"id": "10002", "label": "Task"}]}
  ],
  "warnings": []
}
```

Target можно передать по стабильному `id` или точному `label`; при неоднозначном label провайдер
не угадывает и возвращает warning.

## Generic MCP и SyncService

Registry предоставляет один resolver:

1. выбрать явный `board_type` или единственный настроенный;
2. проверить, что type зарегистрирован и credentials настроены;
3. провалидировать runtime options по spec;
4. создать provider;
5. выполнить операцию;
6. гарантированно вызвать `close()`;
7. санитизировать ошибку.

MCP использует общий lifecycle-wrapper для `sync_board`, `create_task`, `get_board_targets` и
`finish_task`. Общий write-through helper выполняет
`fetch_one → normalize → index_task` после успешной create/finish операции.

Новый публичный API:

- `sync_board(..., board_type, provider_options)`;
- `create_task(..., board_type, project, target, provider_options)`;
- `get_board_targets(board_type, project, provider_options)`;
- `finish_task(..., board_type, target, provider_options)`.

`provider_options` — JSON object, валидируемый schema выбранного spec. Secrets в нём запрещены:
credentials берутся только server-side из env.

Старые параметры `status_field`, `done_state` и `done_column` остаются скрытым compatibility shim
на один релиз:

- YouTrack `status_field` → `provider_options.status_field`;
- `done_state` → generic finish `target`;
- YouGile `done_column` → generic finish `target`.

Новые docstrings, schemas и skills используют только generic форму.

`SyncService` получает уже созданные и настроенные providers. Он сохраняет текущие инварианты:

- отдельный watermark `tasks:<type>:<board>`;
- полный enumerate для active keys;
- `normalize` только выше watermark;
- `normalize_meta` ниже watermark;
- cursor не двигается при `limit`;
- purge использует объединение active keys;
- новый provider не требует изменения sync-алгоритма.

## `.review.yml`

Новая форма:

```yaml
task_board:
  type: jira
  project: PRI
  create_target: To Do
  done_target: Done
  options:
    issue_type: Task
```

YouGile и YouTrack используют те же `create_target`, `done_target` и `options`.
`configure-review` получает labels и choices из `get_board_targets` и не ветвится по типу.

Legacy mapping читается, но не генерируется:

- `done_column` и `done_state` → `done_target`;
- `status_field` → `options.status_field`.

Если одновременно заданы новая и legacy форма, новая имеет приоритет, а validator возвращает
warning о проигнорированном legacy поле.

## Setup credentials

Installer строит board-разделы из `BoardProviderSpec.setup`, поэтому удобный setup обязателен для
каждого зарегистрированного провайдера.

Общий flow:

1. пользователь выбирает зарегистрированный provider;
2. installer показывает официальную инструкцию;
3. открывает браузер только после явного подтверждения;
4. запрашивает secrets скрытым вводом;
5. не печатает secret в preview, diff, error или log;
6. вызывает `validate_connection`;
7. показывает безопасные identity/project/permission metadata;
8. при ошибке сохраняет возможность исправить ввод или продолжить без provider.

### Jira Cloud

- Открыть страницу Atlassian API tokens после подтверждения.
- Предложить **Create API token** без scopes; scoped-token flow требует другого gateway URL и
  `cloudId` и в первый релиз не входит.
- Кратко объяснить имя токена, expiration и необходимость сразу сохранить значение.
- Запросить `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`.
- Проверить `/rest/api/3/myself`, доступ к выбранному project и нужные permissions.
- Использовать Basic auth `email:api-token`; пароль Atlassian не принимается.
- Если пользователь вставил scoped token, validation возвращает безопасную подсказку создать
  совместимый token без scopes, а не маскирует проблему как неверный пароль.

### YouTrack

- Открыть страницу управления permanent tokens выбранного YouTrack instance.
- Напомнить выбрать YouTrack service scope и сохранить полный token с `perm:` prefix.
- Запросить `YOUTRACK_BASE_URL` и `YOUTRACK_TOKEN`.
- Проверить identity и доступ к project.

### YouGile

YouGile REST API v2 не документирует OAuth для внешнего API. OpenID Connect в коробочной версии
решает вход пользователей, а не авторизацию REST-интеграций.

Acquisition hook:

1. скрыто запросить login/password;
2. получить список доступных компаний;
3. дать выбрать компанию;
4. создать API key;
5. немедленно удалить password из локального state;
6. сохранить только `YOUGILE_API_KEY`;
7. проверить доступ и company identity.

Password не записывается в `.env`, transcript, exceptions или logs. При любом сбое installer
отбрасывает password и предлагает официальный ручной flow через интерактивную API-консоль.

Если коробочный YouGile настроен с `allowOnlyOpenId`, acquisition hook не пытается обменять OIDC
session на REST token: документированного OAuth-flow для этого нет. Installer объясняет ограничение
и просит готовый API key, созданный через отдельный API-capable аккаунт с минимальными правами.

## Jira Cloud adapter

`reviewer/tasks/boards/jira.py` реализует REST API v3. Base URL хранится как site URL без
`/rest/api/3`; adapter добавляет API prefix сам.

### Auth и validation

HTTP client использует Basic auth из email и API token. `validate_connection` проверяет текущего
пользователя, видимость project и необходимые browse/create/transition права. Недостающие write
permissions не мешают read-only sync, но setup явно сообщает, какие lifecycle операции недоступны.

### Sync и single read

`iter_raw` вызывает enhanced JQL search
`POST /rest/api/3/search/jql` с project scope и полной пагинацией. Запрашиваются только нужные поля:

- summary и ADF description;
- status и updated;
- subtasks и issue links;
- attachments;
- issue type и project.

Jira `updated` преобразуется в epoch milliseconds для текущего watermark. Полная выдача сохраняется
даже при тёплом cursor, потому что `SyncService` использует все keys для purge.

`fetch_one` использует issue endpoint с тем же набором полей и тот же mapper в `RawTask`;
две code path не расходятся по семантике.

### ADF и `TaskBrief`

Отдельный чистый модуль конвертирует ADF → Markdown и canonical Markdown → ADF.

Минимальный lossless subset для task documents:

- paragraphs и hard breaks;
- headings;
- bullet и ordered lists;
- blockquote и code block;
- strong, emphasis, inline code и links.

Неизвестные ADF nodes не роняют задачу: текст рекурсивно сохраняется, а normalization добавляет
warning. Subtasks и issue links становятся структурированными `criteria`/`links` по текущему
`TaskBrief` контракту.

Attachment metadata берётся из issue fields. Содержимое скачивается существующим helper только с
разрешённого Jira host, с текущими size/timeout/store caps. 403, oversized и неподдерживаемый тип
дают per-attachment warning и не срывают normalization.

### Create

Canonical task Markdown преобразуется в ADF. `project` обязателен. `issue_type` выбирается в
installer/configure-review из project discovery и сохраняется в `provider_options`.

Если `issue_type` отсутствует, sync/read продолжают работать, но create возвращает fail-soft
configuration error вместо угадывания локализованного типа.

После create:

1. adapter получает фактический issue key;
2. если target запрошен, читает доступные transitions;
3. применяет однозначно найденный target;
4. если target недоступен, сохраняет созданную issue в исходном статусе и возвращает warning;
5. generic MCP выполняет write-through reindex.

### Discovery

Project status endpoint даёт статусы по issue types. Discovery возвращает:

- issue types как `options.issue_type.choices`;
- статусы как create/done targets;
- warnings, если пользователь видит project, но не имеет create/transition permission.

Статус из project catalog может оказаться недостижим из конкретного workflow state. Поэтому create
и finish всегда повторно проверяют реальные issue transitions.

### Finish

Finish выполняет частично успешные шаги явно:

1. прочитать текущую issue и ADF description;
2. проверить наличие `pr_url` по link href, а не только по видимому тексту;
3. идемпотентно добавить PR paragraph/link и optional note;
4. если `mark_done`, разрешить target среди текущих transitions и применить его;
5. вернуть отдельные `pr_link_added`, `done_set`, `already_closed` и `warnings`;
6. generic MCP делает write-through независимо от того, удалось ли оба шага выполнить полностью.

Если description обновился, а transition запрещён, результат сохраняет добавленную PR-ссылку и
сообщает permission/transition warning. Откат сетевых side effects не выполняется.

## Ошибки и безопасность

Provider adapters преобразуют транспортные исключения в `BoardProviderError`:

- `configuration`;
- `authentication`;
- `permission`;
- `not_found`;
- `rate_limit`;
- `transient`;
- `unsupported`.

Ошибка содержит только category, безопасное сообщение, actionable hint и retryable-флаг.
HTTP body, request body, URL query с секретами, headers и credential values не входят в `str`,
`repr`, MCP response или logs.

Registry формирует redaction set из всех secret credential fields и legacy aliases. Общий
sanitizer применяется как последний слой перед log/MCP boundary.

401/403 не повторяются. 429 и transient 5xx используют ограниченный backoff с учётом
`Retry-After`; retries не дублируют create/finish без доказанной идемпотентности операции.

## Contract-test suite

Общая suite запускается для YouGile, YouTrack и Jira через adapter fixture и проверяет:

1. registry uniqueness и полноту spec;
2. configured/unconfigured credential resolution;
3. безопасный `validate_connection`;
4. полную pagination в `iter_raw`;
5. стабильный timestamp/watermark mapping;
6. `normalize_meta` без сетевого I/O;
7. Markdown, links, subtasks и attachments;
8. семантическое равенство sync mapper и `fetch_one`;
9. create и точный `target_resolved`;
10. target fallback с warning;
11. нормализованный discovery;
12. идемпотентный finish и PR-link;
13. `close()` на success и error;
14. отсутствие secrets в result, warnings, exceptions и captured logs.

Provider-specific tests дополняют, но не заменяют contract suite.

Jira tests на `httpx.MockTransport` покрывают:

- Basic auth без вывода заголовка;
- enhanced JQL next-page pagination;
- ADF round-trip для canonical task document;
- flatten+warning неизвестного ADF node;
- project issue types и statuses;
- create + transition;
- unreachable и ambiguous transitions;
- attachment 403/oversize/off-host;
- partial finish;
- 401/403/429/5xx sanitation.

YouGile и YouTrack текущие тесты переносятся на те же fixtures без потери существующих assertions.

Отдельный extensibility test регистрирует fake provider и прогоняет
sync/create/discovery/finish через generic MCP без изменения production registry,
`MCPReviewService` или `SyncService`.

Unit suite не использует сеть. Опциональные live smokes имеют маркер `integration`, требуют
отдельные test projects/credentials и никогда не запускаются default `pytest`.

## Installer, CLI, skills и документация

Installer:

- строит board credential fields из registry;
- поддерживает hidden input и provider acquisition hook;
- сохраняет только подтверждённые значения;
- `--dry-run` не открывает браузер, не спрашивает secrets и не вызывает сеть;
- `reviewer check` показывает provider type и результат validation без credential values.

CLI/MCP docstrings говорят «registered board type», а не `yougile|youtrack`.

Skills `sync-tasks`, `solve-task`, `create-task`, `finish-task` и `configure-review` читают generic
`create_target`, `done_target`, `options`; labels и choices приходят из tool result. В skills
остаются только fail-open orchestration rules, не семантика конкретной доски.

README, README.ru.md, CLAUDE.md, `.env.example` и reference docs получают:

- capability matrix YouGile / YouTrack / Jira;
- setup и rotation credentials;
- Jira Cloud limitation;
- добавление adapter/spec/contract fixtures;
- generic `.review.yml`;
- compatibility mapping и срок удаления legacy полей;
- правило: provider считается поддерживаемым только после полной contract suite.

## Миграция и rollout

1. Ввести registry и зарегистрировать неизменённые YouGile/YouTrack adapters.
2. Перевести Settings/factory/SyncService/MCP на registry под существующими тестами.
3. Добавить generic options/targets и compatibility shim.
4. Перевести installer и skills на registry metadata.
5. Добавить contract suite и прогнать её для YouGile/YouTrack.
6. Реализовать Jira adapter и его provider-specific tests.
7. Обновить документацию и capability matrix.
8. Выпустить один compatibility-релиз с warnings для legacy `.review.yml`.
9. Завести отдельную breaking-cleanup задачу после окна миграции.

Rollout не меняет сохранённые задачи, task graph или watermark format. Jira получает собственный
cursor namespace `tasks:jira:<project>`.

## Критерии приёмки

- Доступные типы формируются из зарегистрированных specs, настроенные — из их credential schema.
- В generic factory, MCP и `SyncService` нет ветвлений по YouGile/YouTrack/Jira.
- Fake provider проходит generic MCP lifecycle без изменения generic production modules.
- YouGile и YouTrack сохраняют текущий sync/create/discovery/finish/write-through функционал.
- Jira Cloud поддерживает sync, Markdown/ADF, links, subtasks, attachments, single read, create,
  discovery, finish, PR-link и write-through reindex.
- Jira create не угадывает issue type; wizard/configure-review предлагает project choices.
- Installer удобно получает/проверяет Jira и YouTrack tokens и YouGile API key.
- YouGile password существует только в памяти acquisition hook и отсутствует во всех artifacts.
- Contract suite обязательна и зелена для всех трёх providers.
- API/permission/config errors fail-soft и не содержат credentials.
- Новые CLI, MCP docstrings, `.review.yml` examples и skills не содержат закрытого списка типов.
- Legacy env и config работают в compatibility-релизе с migration warnings.
- Default unit suite остаётся полностью offline.

## Официальные источники

- Jira Cloud REST API v3:
  https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/
- Jira enhanced JQL search:
  https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/
- Jira issue create/edit/transitions:
  https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/
- Jira project statuses:
  https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/
- Atlassian API tokens:
  https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/
- YouTrack permanent tokens:
  https://www.jetbrains.com/help/youtrack/devportal/authentication-with-permanent-token.html
- YouGile REST API v2:
  https://ru.yougile.com/api-v2
- YouGile OpenID Connect:
  https://docs.yougile.com/docs/admin-guide-linux/openid/
