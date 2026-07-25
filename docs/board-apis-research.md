# Публичные API восьми таск-трекеров — факты для адаптеров

Собрано через WebSearch/WebFetch по официальной документации. Каждый факт сопровождён ссылкой на
источник. Где официальную информацию найти не удалось — явно помечено «**НЕ НАЙДЕНО**» с указанием
поисковых запросов. Догадок и невалидированных эндпоинтов нет.

---

## 1. GitHub Issues

### 1. Base URL и версия API
- Cloud: `https://api.github.com`. Версия API передаётся заголовком `X-GitHub-Api-Version`
  (текущая — `2026-03-10`; дефолт без заголовка — `2022-11-28`, поддерживается ≥24 мес. с момента
  выхода следующей версии).
  [API Versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions?apiVersion=2022-11-28)
- **Self-host**: GitHub Enterprise Server — base URL `http(s)://HOSTNAME/api/v3`.
  [Getting started with the REST API (GHES)](https://docs.github.com/en/enterprise-server@3.18/rest/quickstart)

### 2. Аутентификация
- Заголовок `Authorization: Bearer <TOKEN>` (PAT/OAuth/GitHub App installation token — единый формат).
  `Accept: application/vnd.github+json`.
  [REST API endpoints for issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28)
- Org/workspace-идентификатор в заголовках не требуется — контекст задаётся путём `{owner}/{repo}`.

### 3. Список задач + фильтр по времени изменения
- `GET /repos/{owner}/{repo}/issues`
- Параметры: `state` (open/closed/all), `sort` (created/updated/comments), `direction` (asc/desc),
  **`since`** — ISO 8601 timestamp, `labels`, `assignee`, `milestone`.
  [REST API endpoints for issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28)
- **Пагинация**: `page` + `per_page` (max 100, default 30) + HTTP `Link`-заголовок
  (`rel="next"|"prev"|"first"|"last"`); конец — когда `Link` не содержит `rel="next"`.
  [Using pagination in the REST API](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api?apiVersion=2022-11-28)

### 4. Формат описания
- Поле `body` — нативный **Markdown** (GitHub Flavored Markdown). Content negotiation через
  `Accept: application/vnd.github.raw+json` / `...html+json` / `...full+json`.
  [REST API endpoints for issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28)

### 5. Подзадачи / чеклисты
- Нативные **sub-issues** (2024–2025): `GET/POST/DELETE /repos/{owner}/{repo}/issues/{issue_number}/sub_issues`,
  плюс reprioritize и «получить родителя». `sub_issue_id` — числовой ID issue (не номер).
  [REST API endpoints for sub-issues](https://docs.github.com/en/rest/issues/sub-issues) ·
  [A REST API for GitHub Projects, sub-issues improvements](https://github.blog/changelog/2025-09-11-a-rest-api-for-github-projects-sub-issues-improvements-and-more/)
- Чек-листы как таковые — это markdown `- [ ] ...` внутри `body`, отдельного API нет.

### 6. Вложения
- **Скачивание issue-вложений через API не поддерживается** — ни PAT, ни OAuth, ни GitHub App;
  только через браузер UI.
  [GitHub Issue Attachments Cannot Be Downloaded via API or PAT](https://codenote.net/en/posts/github-issue-attachments-download-api-unsupported/) ·
  подтверждение в обсуждении: [any way to upload files to an existing github issue by REST API #46951](https://github.com/community/community/discussions/46951)
- Список комментариев (могут содержать markdown-ссылки на вложения):
  `GET /repos/{owner}/{repo}/issues/{issue_number}/comments`.
  [REST API endpoints for issue comments](https://docs.github.com/en/rest/issues/comments)

### 7. Чтение по ключу / человекочитаемый ключ
- Ключ — `#{issue_number}` (репо-скоуп). Fetch: `GET /repos/{owner}/{repo}/issues/{issue_number}`.
  [REST API endpoints for issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28)

### 8. Discovery целевых состояний
- Базовый статус — поле `state` (`open`/`closed`), опционально `state_reason`.
- Milestones: `GET /repos/{owner}/{repo}/milestones` (state/sort/direction/per_page/page),
  создание — `POST /repos/{owner}/{repo}/milestones` (обязателен `title`; `state` enum `open`/`closed`).
  [REST API endpoints for milestones](https://docs.github.com/en/rest/issues/milestones?apiVersion=2022-11-28)
- Labels — `GET /repos/{owner}/{repo}/labels`, применение — `labels` в теле issue.
- Установка значения — `PATCH /repos/{owner}/{repo}/issues/{issue_number}` (`state`, `labels`, `milestone`).

### 9. Создание задачи
- `POST /repos/{owner}/{repo}/issues`. Обязательно: `title`. Опционально: `body`, `assignees`,
  `labels`, `milestone`. «Куда создавать» — `{owner}/{repo}` в пути.
  [Getting started results / REST API endpoints for issues](https://docs.github.com/en/rest/issues/issues)

### 10. Rate limits
- Стандартные аутентифицированные запросы: **5000 req/hour**; GitHub App installation —
  мин. 5000, макс. 12500 (масштабируется от числа репо/пользователей); Enterprise Cloud
  OAuth/GitHub App, принадлежащий организации — 15000 req/hour; `GITHUB_TOKEN` в Actions —
  1000 req/hour на репозиторий (15000 для Enterprise Cloud).
- Заголовки: `x-ratelimit-limit`, `x-ratelimit-remaining`, `x-ratelimit-used`, `x-ratelimit-reset`
  (UTC epoch), `x-ratelimit-resource`.
- Превышение: `403` или `429`; secondary rate limit — проверять `retry-after`, иначе ждать ≥1 мин.
  [Rate limits for the REST API](https://docs.github.com/en/rest/overview/rate-limits-for-the-rest-api?apiVersion=2022-11-28)

### 11. Подводные камни
- Вложения issue **невозможно** скачать программно — важно для пункта «вложения» адаптера (fallback:
  парсить markdown-ссылки на `user-images.githubusercontent.com`/`github.com/.../files/...`, но сами
  файлы там публично отдаются по прямой ссылке без токена в большинстве случаев — уточнять отдельно).
- `sub_issue_id` — не номер issue, а внутренний numeric ID; нужен доп. запрос для маппинга.
- API-версия передаётся заголовком, а не в URL — при интеграции нужно явно фиксировать
  `X-GitHub-Api-Version`, иначе поведение может тихо меняться при обновлении дефолтной версии GitHub.
- `Content-Type` для JSON-тела — `application/json`; `Accept` обязателен для корректного версионирования.

---

## 2. Trello

### 1. Base URL и версия API
- `https://api.trello.com/1/` (версия — `1`, в пути). Только SaaS, self-host не предусмотрен
  (продукт Atlassian Cloud).
  [Trello REST API reference](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/)

### 2. Аутентификация
- **Query-параметры**, не заголовок: `?key=<API_KEY>&token=<API_TOKEN>`.
  [Trello REST API reference](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/)
- Обязательные креды: `key` (per-приложение) + `token` (per-пользователь). Org/workspace ID не
  требуется — контекст через `idBoard`/`idOrganization` в пути/параметрах конкретных запросов.

### 3. Список задач + фильтр по времени изменения
- Список карточек доски: `GET /1/boards/{id}/cards` (или с `{filter}` в пути:
  `GET /1/boards/{id}/cards/{filter}`).
  [Nested Resources](https://developer.atlassian.com/cloud/trello/guides/rest-api/nested-resources/)
- **Важная особенность**: официального `since=updated_at`-фильтра для эндпоинта карточек **нет** —
  параметры `since`/`before` в Trello REST относятся к дате **создания** карточки/action, а не к
  модификации. Известное ограничение, подтверждённое сообществом разработчиков; есть недокументированный
  ad-hoc параметр `cards_modifiedSince` при запросе карточек как nested-ресурса доски
  (`GET /1/boards/{id}?cards=open&cards_modifiedSince=...`), но он не поддерживает сортировку/лимит.
  [Fetching cards changed since ts — Atlassian Developer Community](https://community.developer.atlassian.com/t/fetching-cards-changed-since-ts/34941)
- Практический путь синка: поле `dateLastActivity` есть в каждой карточке — вычитывать все карточки
  доски и фильтровать/сортировать на своей стороне.
  [Object definitions — Card](https://developer.atlassian.com/cloud/trello/guides/rest-api/object-definitions/)
- **Пагинация**: query `limit` (per request); для этого конкретного endpoint явный `page`/`before`
  в официальной доке не документирован детально — рекомендовано use Postman collection/OpenAPI-спеку.
  [Cards API navigation](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/) —
  частично **НЕ НАЙДЕНО** (искал: "Trello API cards since before pagination since parameter incremental sync").

### 4. Формат описания
- Поле `desc` — **plain text**, до 16384 символов (Trello поддерживает базовый markdown-подобный
  синтаксис в UI, но в самой доке поле описано просто как текстовое, без спецификации markdown-грамматики).
  [Card object definition](https://developer.atlassian.com/cloud/trello/guides/rest-api/object-definitions/)

### 5. Подзадачи / чеклисты
- `GET /cards/{id}/checklists` — список чеклистов; `POST /cards/{id}/checklists` — создать;
  `GET/PUT/DELETE /cards/{id}/checkItem/{idCheckItem}` — работа с пунктами.
  [Trello Cards API](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/)

### 6. Вложения
- `GET /cards/{id}/attachments` — список; `POST /cards/{id}/attachments` — добавить;
  `DELETE /cards/{id}/attachments/{idAttachment}` — удалить.
  [Trello Cards API](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/)
- Скачивание: `GET /1/cards/{id}/attachments/{idAttachment}/download/{filename}` —
  **требует авторизацию** (`key`+`token`, либо `Authorization: OAuth oauth_customer_key="...", oauth_token="..."`);
  публичного анонимного доступа к файлу нет.
  [Download attachments with API — Atlassian Community](https://community.developer.atlassian.com/t/download-attachments-with-api/72386)

### 7. Чтение по ключу / человекочитаемый ключ
- Человекочитаемый ключ — `shortLink` (8-символьный) или `shortUrl`/`url`.
  `GET /1/cards/{id}` принимает и полный `id`, и `shortLink` (community-подтверждено; официальная
  типизация параметра — `TrelloID`, но практически принимается shortlink).
  [Cards API — Get a Card](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/#api-cards-id-get) ·
  [REST API - Get Card by short id — Atlassian Community](https://community.developer.atlassian.com/t/rest-api-get-card-by-short-id/43756)

### 8. Discovery целевых состояний
- Роль статуса выполняет **список (list)**: `GET /1/boards/{id}/lists` — все списки доски.
  [Get Lists on a Board](https://developer.atlassian.com/cloud/trello/rest/api-group-lists/)
- Установка — `PUT /1/cards/{id}?idList=<idList>` (переместить карточку в список = сменить статус).

### 9. Создание задачи
- `POST /1/cards`. Обязательно: `idList`. Опционально: `name`, `desc`, `pos`, `due`, `start`,
  `dueComplete`, `idMembers`, `idLabels`, `urlSource`. «Куда создавать» — `idList` (доска выводится
  из списка).
  [Trello Cards API](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/)

### 10. Rate limits
- **300 запросов/10 сек на API key**, **100 запросов/10 сек на token**; отдельно
  `/1/members/*` — 100 запросов/900 сек.
- Заголовки: `x-rate-limit-api-token-interval-ms`, `x-rate-limit-api-token-max`,
  `x-rate-limit-api-token-remaining`, `x-rate-limit-api-key-interval-ms`, `x-rate-limit-api-key-max`,
  `x-rate-limit-api-key-remaining`.
- Превышение — `429`.
  [API Rate Limits — Atlassian Support](https://support.atlassian.com/trello/docs/api-rate-limits/) ·
  [Rate Limits — Trello Developer Guides](https://developer.atlassian.com/cloud/trello/guides/rest-api/rate-limits/)

### 11. Подводные камни
- Нет серверного фильтра по `updated_at` для карточек — единственный надёжный способ инкрементального
  синка «из коробки» — full pull + локальная фильтрация по `dateLastActivity` (либо polling `actions`
  endpoint, который **has** `since`/`before` по дате action, но это другой ресурс).
- Аутентификация — query-параметры, а не заголовок (риск утечки в логах прокси/веб-серверов).
- Self-host отсутствует — только Atlassian Cloud SaaS.

---

## 3. Linear

### 1. Base URL и версия API
- GraphQL endpoint: `https://api.linear.app/graphql`. Версии API как таковой (в духе REST
  `X-Api-Version`) в схеме не выделяется — единая эволюционирующая GraphQL-схема.
  [Getting started — Linear Developers](https://linear.app/developers/graphql)
- Self-host — не предусмотрен (SaaS-only).

### 2. Аутентификация
- **Personal API key**: `Authorization: <API_KEY>` — **без** префикса `Bearer`.
- **OAuth2 access token**: `Authorization: Bearer <ACCESS_TOKEN>` — префикс `Bearer` обязателен.
  [Getting started — Linear Developers](https://linear.app/developers/graphql)
- Workspace-идентификатор отдельным заголовком не передаётся — определяется токеном.

### 3. Список задач + фильтр по времени изменения
- GraphQL query `issues(...)`. Фильтр: `filter: { updatedAt: { gt: "<ISO8601>" } }`
  (компараторы `lt`/`lte`/`gt`/`gte`).
  [Filtering — Linear Developers](https://linear.app/developers/filtering)
- Сортировка: `orderBy: updatedAt` (по умолчанию — `createdAt`).
  [Pagination — Linear Developers](https://linear.app/developers/pagination)
- **Пагинация** — курсорная: `first`/`after` (форвард), `last`/`before` (назад); ответ содержит
  `pageInfo { hasNextPage, endCursor }`. Дефолт — 50 результатов без явных аргументов.
  [Pagination — Linear Developers](https://linear.app/developers/pagination)

### 4. Формат описания
- Поле `description` — **Markdown** (подтверждено: изображения `![]()`, коллапс-секции `+++`,
  упоминания через голый URL — всё markdown-синтаксис).
  [Create issues — Linear Docs](https://linear.app/docs/creating-issues)

### 5. Подзадачи / чеклисты
- Родное дерево sub-issues через `parentId` при создании (`issueCreate(input: { parentId: ... })`)
  и `Issue.parent`. Обратное поле для чтения дочерних issues в схеме называется `children`
  (Apollo-схема Linear API — **не подтверждено официальной prose-докой напрямую**, только по схеме
  и community-примерам конвертации в sub-issue via `convertIssueToSubtask` мутации) —
  частично **НЕ НАЙДЕНО** (искал: "linear.app/developers sdk issue.children children() sub-issues",
  официальный текст доки не показал имя поля явно).
  [Parent and sub-issues — Linear Docs](https://linear.app/docs/parent-and-sub-issues) ·
  [beads issue #1528 (community, parentId)](https://github.com/steveyegge/beads/issues/1528)

### 6. Вложения
- Ресурс `Attachment`, привязан к issue через `issueId`, идентифицируется `url` (idempotent — повторное
  создание с тем же `url` на том же issue обновляет существующий, а не дублирует).
  [Attachments — Linear Developers](https://linear.app/developers/attachments)
- Файлы, загруженные через `fileUpload`-мутацию, хранятся в приватном облаке Linear — **скачивание
  требует аутентификации** ("must authenticate to access these files elsewhere").
  [How to upload a file to Linear — Linear Developers](https://linear.app/developers/how-to-upload-a-file-to-linear)

### 7. Чтение по ключу / человекочитаемый ключ
- Человекочитаемый ключ — `identifier` вида `ENG-123` (`TEAM-NUMBER`).
- GraphQL query `issue(id: $id)` официально типизирован под internal `id` (UUID); фактическую выборку
  по `identifier` большинство клиентов делает через `filter: { number: {eq: ...}, team: {key:{eq:...}} }`
  либо `issueSearch`/через сам `identifier` как алиас (в практике Linear API `issue(id:)` также
  принимает identifier-строку — подтверждено в сторонних примерах, но не найдено явного текста
  в prose-документации, разделяющего UUID/identifier).
  [Getting started — Linear Developers](https://linear.app/developers/graphql) ·
  частично **НЕ НАЙДЕНО** (искал: официальное подтверждение, что `issue(id:)` принимает identifier
  формата ENG-123 наравне с UUID).

### 8. Discovery целевых состояний
- `WorkflowState` — команда-скоупленные статусы (`type` бывает started/completed/canceled/etc.);
  запрос списка — `team.states` / `workflowStates(filter: {team: {id: {eq: ...}}})`.
  Смена статуса — `issueUpdate(id: ..., input: { stateId: <UUID> })`.
  [Endgrate: How to Create Or Update Issues with the Linear API](https://endgrate.com/blog/how-to-create-or-update-issues-with-the-linear-api-in-python) —
  вторичный источник, официальная страница workflow states отдельным prose-документом не
  зафиксирована в ходе поиска; структура полей подтверждена по практике SDK.

### 9. Создание задачи
- `issueCreate(input: { title: ..., teamId: ... })`. Обязательно: `title`, `teamId`. Опционально:
  `description`, `parentId`, `stateId`, `assigneeId`, `labelIds`.
  [Fetching & modifying data — Linear Developers](https://linear.app/developers/sdk-fetching-and-modifying-data)

### 10. Rate limits
- Аутентифицированные (API key): **5000 req/hour**; неаутентифицированные: 600 req/hour /
  100000 complexity points/hour.
- Complexity-based: макс. сложность одного запроса — 10000 points; общий бюджет API key —
  250000 complexity points/hour на пользователя.
- Заголовки: `X-RateLimit-Requests-Limit/Remaining/Reset`, endpoint-specific
  `X-RateLimit-Endpoint-*`, complexity `X-Complexity`, `X-RateLimit-Complexity-Limit/Remaining/Reset`.
- Превышение: HTTP **400** (не 429!) с `errors[].extensions.code == "RATELIMITED"` в теле GraphQL-ответа.
  [Rate limiting — Linear Developers](https://linear.app/developers/rate-limiting)

### 11. Подводные камни
- Rate-limit превышение возвращает **400**, а не 429 — нужно парсить тело ответа, HTTP-статус
  недостаточен для детекции.
- Personal API key **без** `Bearer`-префикса — частая ошибка интеграторов (OAuth token — c префиксом).
- Вложения из приватного облака Linear требуют токен даже на скачивание — нельзя просто взять `url`.

---

## 4. ClickUp

### 1. Base URL и версия API
- `https://api.clickup.com/api/v2` — API v2. Есть частичный v3 (например, для Attachments —
  "V3 Attachments API поддерживает и tasks, и File type Custom Fields").
  [Get Task](https://developer.clickup.com/reference/gettask) ·
  [Create Task Attachment](https://developer.clickup.com/reference/createtaskattachment)
- Self-host не предусмотрен — SaaS-only.

### 2. Аутентификация
- Personal API token: `Authorization: {personal_token}` — **без** `Bearer`; токен с префиксом `pk_`.
- OAuth access token: `Authorization: Bearer {access_token}` — **с** `Bearer`.
  [Authentication — ClickUp Developer Docs](https://developer.clickup.com/docs/authentication)
- Team/workspace ID передаётся как явный query/path-параметр там, где нужен (`team_id`), не заголовком.

### 3. Список задач + фильтр по времени изменения
- `GET /list/{list_id}/task`. Пагинация: **`page`** (0-индексная, "starts at 0"), ответ ограничен
  **100 задач/страница**.
- Фильтр по обновлению: **`date_updated_gt`** / **`date_updated_lt`** — Unix time в **миллисекундах**.
  Сортировка: `order_by` (id/created/updated/due_date), `reverse` (bool).
  [Get Tasks](https://developer.clickup.com/reference/gettasks)

### 4. Формат описания
- Поле `description` (plain text) по умолчанию; параметр запроса
  **`include_markdown_description=true`** возвращает описание в **Markdown**
  (аналогично на create/update через `markdown_content`/`markdown_description`, судя по практике SDK).
  [Get Tasks](https://developer.clickup.com/reference/gettasks) ·
  [Get Task](https://developer.clickup.com/reference/gettask)

### 5. Подзадачи / чеклисты
- Subtasks: поле `parent` при создании (`POST /list/{list_id}/task`, body `parent: "<task_id>"`);
  чтение — `include_subtasks=true` на Get Task / Get Tasks.
- Чеклисты: `POST /task/{task_id}/checklist` — создать чеклист (обязателен `name`); пункты чеклиста —
  через отдельный "Create Checklist Item" эндпоинт (не тот же вызов).
  [Create Checklist](https://developer.clickup.com/reference/createchecklist) ·
  [Get Task](https://developer.clickup.com/reference/gettask)

### 6. Вложения
- `POST /task/{task_id}/attachment` — загрузка (`multipart/form-data`, только локальные файлы,
  облачные ссылки не принимаются). Есть новый V3 Attachments API (поддерживает tasks + File custom
  fields).
  [Create Task Attachment](https://developer.clickup.com/reference/createtaskattachment)
- Ответ Get Task содержит поле `attachments` с уже готовыми URL (сами карточки задачи возвращают
  вложения инлайн) — точная информация о необходимости авторизации при скачивании
  **НЕ НАЙДЕНО** (искал: "clickup task attachment url download authentication required signed url").

### 7. Чтение по ключу / человекочитаемый ключ
- Обычный `task_id` — буквенно-цифровой (не число). Человекочитаемый **custom ID** поддерживается
  через `custom_task_ids=true&team_id=<id>` в query — тогда `task_id` в пути трактуется как кастомный
  ключ задачи.
  [Get Task](https://developer.clickup.com/reference/gettask)

### 8. Discovery целевых состояний
- Роль статуса — **statuses списка**: `GET /list/{list_id}` возвращает список, содержащий (по
  косвенным данным SDK/wrapper-библиотек) массив `statuses` со статус-объектами (`status`, `type`
  open/custom/closed, `color`, `orderindex`) — **точная официальная response-схема поля `statuses`
  НЕ НАЙДЕНО** через прямой fetch офиц. страницы (интерактивная документация не отдаёт статичный
  пример без выполнения запроса); подтверждено косвенно через практику клиентских библиотек
  (clickupython `SingleList.statuses: List[StatusElement]`).
  [Get List](https://developer.clickup.com/reference/getlist) ·
  [clickupython docs — Lists](https://clickupython.readthedocs.io/en/latest/lists.html)
- Установка — `PUT /task/{task_id}` body `{"status": "<status name string>"}` (имя должно совпадать
  с одним из статусов, настроенных в списке).
  [Get Tasks / Tasks doc](https://developer.clickup.com/docs/tasks)

### 9. Создание задачи
- `POST /list/{list_id}/task`. Обязательно: path `list_id`, body `name`. Опционально: `description`,
  `status`, `assignees`, `priority`, `tags`, `due_date`, `parent` (для subtask), `custom_fields`.
  [Create Task](https://developer.clickup.com/reference/createtask)

### 10. Rate limits
- Free/Unlimited/Business: **100 req/min на токен**; Business Plus: **1000 req/min**;
  Enterprise: **10000 req/min**.
- Заголовки: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.
- Превышение — `429`, уважать `Retry-After`.
  [Rate Limits — ClickUp Developer Docs](https://developer.clickup.com/docs/rate-limits)

### 11. Подводные камни
- Кастомные поля (`custom_fields`) сохраняются в теле создания задачи только если применимы к
  `custom_item_id` данного типа задачи — нужно предварительно свериться с Get Custom Fields.
  [Create Task](https://developer.clickup.com/reference/createtask)
- Два разных auth-формата (личный токен без Bearer, OAuth — с Bearer) — легко перепутать.
- `date_updated_gt/lt` — миллисекунды, а не секунды (частая ошибка).

---

## 5. Asana

### 1. Base URL и версия API
- `https://app.asana.com/api/1.0` (версия `1.0` в пути).
  [Get a task — Asana Developers](https://developers.asana.com/reference/gettask)
- Self-host не предусмотрен — SaaS-only.

### 2. Аутентификация
- PAT и OAuth — единый формат: `Authorization: Bearer <token>` (по документации авторизации,
  оба метода используют стандартный bearer-токен).
  [Authentication — Asana Developers](https://developers.asana.com/docs/authentication)
- Обязательный `workspace` (gid) как параметр запроса для многих list/create операций, если не
  указан `project`/`parent`.
- Явного специального заголовка типа `Asana-Enable` в исследованных страницах **не обнаружено**
  (устаревшая практика opt-in feature flags через этот заголовок существовала в API v1.0 в
  прошлом, но в текущей актуальной доке авторизации о нём не упоминается) —
  частично **НЕ НАЙДЕНО** (искал текущий статус `Asana-Enable` header в актуальной документации).

### 3. Список задач + фильтр по времени изменения
- `GET /tasks`. Обязательно указать **либо** `project`, **либо** `workspace`+`assignee` вместе.
- Фильтр по времени: **`modified_since`** — ISO 8601 datetime (только задачи, изменённые после);
  также `completed_since`.
- **Пагинация**: `limit` (1–100), `offset` (opaque cursor-токен, **истекает** через некоторое время —
  повторно использовать нельзя); ответ включает `next_page: { offset, path, uri }`.
  [Pagination — Asana Developers](https://developers.asana.com/docs/pagination) ·
  практика подтверждена через [forum: exporting all modified tasks efficiently](https://forum.asana.com/t/exporting-all-modified-tasks-efficiently/133733)

### 4. Формат описания
- **`notes`** — plain text ("More detailed, free-form textual information associated with the task").
- **`html_notes`** — тот же контент в виде **HTML** (Asana-flavored, opt-in через `opt_fields`),
  напр. `<body>Mittens <em>really</em> likes...</body>`.
  [asana-api-meta task.yaml (официальный источник схемы Asana)](https://raw.githubusercontent.com/Asana/asana-api-meta/master/src/resources/task.yaml)

### 5. Подзадачи / чеклисты
- `GET /tasks/{task_gid}/subtasks` (`getSubtasksForTask`) — параметры `task_gid`, `opt_fields`,
  `limit`, `offset`.
  [node-asana AttachmentsApi/TasksApi docs (реф. на офиц. API)](https://github.com/Asana/node-asana)

### 6. Вложения
- `GET /attachments?parent={object_gid}` (`getAttachmentsForObject`) — `object_gid`, `limit`,
  `offset`, `opt_fields`. Объект `Attachment` = любой файл, включая связанные через Dropbox/Google
  Drive.
  [node-asana AttachmentsApi.md](https://github.com/Asana/node-asana/blob/master/docs/AttachmentsApi.md)

### 7. Чтение по ключу / человекочитаемый ключ
- Ключ — числовой `gid` (никакого человекочитаемого short-key нет, в отличие от Linear/GitHub).
  `GET /tasks/{task_gid}`.
  [Get a task](https://developers.asana.com/reference/gettask)

### 8. Discovery целевых состояний
- Роль статуса — **секции (Sections)** проекта + boolean-поле `completed` на задаче.
- `GET /projects/{project_gid}/sections` (`getSectionsForProject`) — список секций.
- Установка секции — `POST /sections/{section_gid}/addTask` (`addTaskForSection`, требует
  scope `tasks:write`; перемещает задачу из других секций проекта, по умолчанию — в начало секции).
  Установка `completed` — `PUT /tasks/{task_gid}` body `{"completed": true}`.
  [Sections — Asana Developers](https://developers.asana.com/reference/sections) ·
  [node-asana SectionsApi.md](https://github.com/Asana/node-asana/blob/master/docs/SectionsApi.md)

### 9. Создание задачи
- `POST /tasks`. Обязательно: `workspace` **либо** `projects`/`parent` (тогда workspace выводится
  автоматически). После создания `workspace` неизменяем.
  [Create a task](https://developers.asana.com/reference/createtask)

### 10. Rate limits
- **Free**: 150 req/min; **Paid**: 1500 req/min; **Search API**: отдельный лимит 60 req/min;
  конкурентные задачи (дублирование/export): максимум 5 на пользователя.
- Конкурентные запросы: до 50 одновременных **GET**, до 15 одновременных **POST/PUT/PATCH/DELETE**
  (независимые лимиты read/write).
- Есть отдельный **cost-based limiter** (граф-обход, тяжёлые запросы могут упереться в лимит даже
  при малом числе запросов).
- Превышение — **429**, заголовок **`Retry-After`** (секунды); отклонённые запросы всё равно тратят
  квоту.
  [Rate limits — Asana Developers](https://developers.asana.com/docs/rate-limits)

### 11. Подводные камни
- `offset`-токен пагинации может истечь между запросами (если данные изменились) — нельзя
  полагаться на него как на долгоживущий курсор.
- `html_notes` — **не Markdown**, а собственный HTML-flavour Asana; при записи через API также
  нужно использовать `html_notes` в специфичном формате, а не произвольный HTML.
- Rate limiting «стоимостной» (cost-based) поверх обычного req/min — тяжёлые `opt_fields`-запросы
  могут троттлиться раньше, чем ожидается по одному лишь числу запросов.

---

## 6. Yandex Tracker

### 1. Base URL и версия API
- `https://api.tracker.yandex.net` — актуальные примеры в доке даны на **v3** (`/v3/issues/_search`,
  `/v3/issues/{id}`), часть легаси-эндпоинтов — v2 (напр. attachments: `/v2/issues/{id}/attachments`).
  [Find issues](https://yandex.ru/support/tracker/en/api-ref/issues/search-issues) ·
  [Attach a file](https://cloud.yandex.com/en/docs/tracker/concepts/issues/post-attachment)
- Self-host: официальной информации о self-hosted версии Yandex Tracker **НЕ НАЙДЕНО** (искал:
  "Yandex Tracker self-hosted on-premise"); продукт позиционируется как облачный сервис в составе
  Yandex 360/Yandex Cloud.

### 2. Аутентификация
- **OAuth**: `Authorization: OAuth <OAuth-токен>`.
- **IAM-токен** (только для организаций на Yandex Cloud, включая сервисные аккаунты):
  `Authorization: Bearer <IAM-токен>`, время жизни IAM-токена — не более 12 часов.
- **Org ID заголовок обязателен всегда, вместе с токеном**:
  - **`X-Org-ID`** — если Tracker-организация привязана к **Яндекс 360 для бизнеса**.
  - **`X-Cloud-Org-ID`** — если Tracker-организация привязана к **Yandex Cloud Organization**
    (ID берётся в Administration → Organizations).
  [API access — Yandex Tracker](https://yandex.ru/support/tracker/en/api-ref/access) ·
  [Create an issue (X-Cloud-Org-ID mention)](https://yandex.ru/support/tracker/en/api-ref/issues/create-issue)

### 3. Список задач + фильтр по времени изменения
- `POST /v3/issues/_search`. Приоритет параметров тела: `queue` → `keys` → `filter` (map полей,
  включая `updatedAt`/`updated`) → `query` (язык запросов Tracker, поддерживает
  `"Sort by": Updated DESC` и т.п.).
  [Find issues](https://yandex.ru/support/tracker/en/api-ref/issues/search-issues)
- **Пагинация**:
  - Стандартная (<10000 результатов): `perPage` (default 50), заголовок ответа `Link`
    (`rel="next"`), `X-Total-Count`.
  - Scroll-режим (>10000 результатов): query `scrollType` (`sorted`/`unsorted`), `perScroll`
    (default 100, max 1000), `scrollTTLMillis` (default 60000); ответ содержит заголовки
    **`X-Scroll-Id`** и **`X-Scroll-Token`**, которые передаются в след. запросе как `scrollId`/
    `scrollToken`.
  [Find issues](https://yandex.ru/support/tracker/en/api-ref/issues/search-issues)

### 4. Формат описания
- Поле **`description`**. Форматирование — **YFM (Yandex Flavored Markdown)**; при создании нужно
  явно указать `markupType: "md"`, если используется YFM-разметка.
  [Create an issue](https://yandex.ru/support/tracker/en/api-ref/issues/create-issue)

### 5. Подзадачи / чеклисты
- Иерархия: поле `parent` (объект/строка) при создании; связи `links` с типами
  `"is subtask for"` / `"is parent task for"`.
- Чек-листы: `POST /v3/issues/{issue_ID}/checklistItems` — создать чеклист/добавить пункты
  (параметры: текст, `checked`, исполнитель, дедлайн).
  [Create an issue](https://yandex.ru/support/tracker/en/api-ref/issues/create-issue) ·
  [Creating a checklist or adding items to it](https://yandex.ru/support/tracker/en/concepts/issues/add-checklist-item)

### 6. Вложения
- `POST /v2/issues/{issue-id}/attachments/?filename=<name>` — прикрепить (multipart/form-data,
  лимит размера — 1024 Мб).
- `GET /v2/issues/{issue-id}/attachments` — список прикреплённых файлов.
- Авторизация при скачивании — те же заголовки (`Authorization` + `X-Org-ID`/`X-Cloud-Org-ID`),
  явного упоминания анонимного доступа нет.
  [Attach a file](https://cloud.yandex.com/en/docs/tracker/concepts/issues/post-attachment) ·
  [Get a list of attached files](https://tech.yandex.com/connect/tracker/api/concepts/issues/get-attachments-list-docpage/)

### 7. Чтение по ключу / человекочитаемый ключ
- Человекочитаемый ключ — `<QUEUE>-<NUMBER>` (напр. `TREK-9844`). `GET /v3/issues/{issue_ID}` —
  `issue_ID` принимает и числовой ID, и ключ-строку (подтверждено примерами в доке: `"JUNE-3"`).
  [Get an issue](https://yandex.ru/support/tracker/en/api-ref/issues/get-issue)

### 8. Discovery целевых состояний
- Роль статуса — **статусы очереди + переходы (transitions)**.
- `GET /v3/issues/{issue_ID}/transitions` — доступные переходы для задачи; каждый переход содержит
  объект `to` (`id`, `key`, `display`) — целевой статус.
- Выполнение перехода — `POST /v3/issues/{issue_ID}/transitions/{transitionId}/_execute`.
  [Get transitions](https://yandex.ru/support/tracker/en/api-ref/issues/get-transitions)

### 9. Создание задачи
- `POST /v3/issues/`. Обязательно: **`summary`**, **`queue`** (объект/строка ключа очереди/число ID).
  Опционально: `description` (+`markupType`), `parent`, `links`.
  [Create an issue](https://yandex.ru/support/tracker/en/api-ref/issues/create-issue)

### 10. Rate limits
- **НЕ НАЙДЕНО** — официальные числовые лимиты запросов/сек или заголовки `X-RateLimit-*` для
  Yandex Tracker API в документации не обнаружены (искал: "Yandex Tracker API rate limit лимит
  запросов в секунду 429", "Yandex Tracker API X-RateLimit"). Общая практика Yandex Cloud API
  подразумевает квоты, но специфичных для Tracker чисел найти не удалось.

### 11. Подводные камни
- **Два разных org-заголовка** — использование не того (`X-Org-ID` вместо `X-Cloud-Org-ID` или
  наоборот) для типа организации приведёт к ошибке авторизации; нужно заранее знать тип организации
  клиента.
- IAM-токен (Bearer) работает **только** в организациях на Yandex Cloud, не в Яндекс 360 —
  для последних доступен только OAuth.
- YFM ≠ обычный Markdown — требует явного `markupType: "md"`, иначе возможна иная интерпретация
  разметки.
- Версии эндпоинтов смешаны (v2 для attachments, v3 для issues/search/transitions) — уточнять
  версию per-эндпоинт, а не считать API монолитно версионированным.

---

## 7. Kaiten

> Внимание: в поиске также всплывает **kaiten.sh** — это **другой, несвязанный** open-source
> продукт ("Kaiten — Open Source Unified SaaS Control Plane"), омонимичное название. Ниже —
> только про **kaiten.ru** (российский таск-трекер, `developers.kaiten.ru`), как и требовалось.

### 1. Base URL и версия API
- Company-scoped subdomain: **`https://<your_domain>.kaiten.ru/api/v1`**, актуальная версия также
  доступна по алиасу **`https://<your_domain>.kaiten.ru/api/latest`**.
  [REST API — Kaiten Developer Documentation](https://developers.kaiten.ru/)
- **Self-host**: да — отдельный продукт **Kaiten On-Premises** (enterprise), полностью на
  инфраструктуре заказчика, есть отдельный SLA-документ.
  [On-premises — Kaiten](https://enterprise.kaiten.ru/) ·
  [SLA по технической поддержке Kaiten On-Premises](https://enterprise.kaiten.ru/sla)

### 2. Аутентификация
- `Authorization: Bearer <token>`. Токен создаётся в настройках профиля пользователя (API key).
  [REST API — Kaiten Developer Documentation](https://developers.kaiten.ru/)
- Org/space-идентификатор отдельным заголовком не передаётся — контекст в subdomain + `board_id`/
  `space_id` в параметрах запросов.

### 3. Список задач + фильтр по времени изменения
- `GET /api/latest/cards`. Фильтры по времени: **`updated_after`** / **`updated_before`**
  (ISO 8601), плюс `created_after/before`, `due_date_after/before`,
  `first_moved_in_progress_after/before`, `last_moved_to_done_at_after/before`.
- Прочие фильтры: `board_id`, `space_id`, `column_id`, `lane_id`, `member_ids`, `owner_ids`,
  `responsible_ids`, `tag_ids`, `type_ids`, `exclude_board_ids/lane_ids/column_ids`,
  `states` (1-queued/2-inProgress/3-done), `condition` (1-live/2-archived).
- **Пагинация**: `limit` (default 100, max 100), `offset`; альтернативно `version=2` включает
  OpenSearch-based ответ с курсором `start_position`.
- Сортировка: `order_by` (список полей через запятую) + `order_direction` (`asc`/`desc`,
  позиционно соответствует `order_by`).
  [Retrieve card list — Kaiten Developer Documentation](https://developers.kaiten.ru/cards/retrieve-card-list)

### 4. Формат описания
- Поле **`description`** — plain text/nullable string (markdown-специфика в самой доке
  эндпоинта не заявлена явно; Kaiten UI поддерживает rich-text редактор, но формат хранения через
  API не уточнён отдельным заявлением) — частично **НЕ НАЙДЕНО** (искал точную спецификацию формата
  `description`, markdown vs HTML, в доке карточек Kaiten).
  [Retrieve card — Kaiten Developer Documentation](https://developers.kaiten.ru/cards/retrieve-card)

### 5. Подзадачи / чеклисты
- Есть поле `checklists` (массив) прямо в объекте карточки + `parent_checklist_ids`.
- Отдельные ресурсы: **Card Checklists** (`GET /card-checklists/retrieve-card-checklist`) и
  **Checklist Items** / **Card Checklist Items** (`POST/PATCH/DELETE .../add-item-to-checklist`,
  `update-checklist-item`, `remove-checklist-item`).
  [Retrieve card](https://developers.kaiten.ru/cards/retrieve-card) ·
  навигация API: card-checklists / card-checklist-items / checklist-items разделы на
  [developers.kaiten.ru](https://developers.kaiten.ru/)

### 6. Вложения
- `PUT /card-files/attach-file-to-card`, `PATCH /card-files/update-file`,
  `DELETE /card-files/detach-file-from-card` — управление файлами карточки.
- Отдельного явного **GET-списка** файлов карточки в навигации не обнаружено — вероятно, файлы
  возвращаются инлайн в ответе `GET /cards/{card_id}` (по аналогии с `checklists`); есть также
  бета-раздел **Private Card Files** (`POST attach / GET get-card-file / DELETE delete-card-file`).
  Требуется ли авторизация при скачивании файла — **НЕ НАЙДЕНО** (искал: "kaiten card file download
  url authentication required").
  [developers.kaiten.ru навигация (card-files, private-card-files)](https://developers.kaiten.ru/)

### 7. Чтение по ключу / человекочитаемый ключ
- Ключ — числовой `id` карточки; есть также `uid`-подобные строковые идентификаторы для колонок.
  Явного человекочитаемого short-key (по аналогии с GitHub `#123`) для карточек в исследованных
  страницах не обнаружено — карточки идентифицируются числовым `id`.
  `GET /api/latest/cards/{card_id}`.
  [Retrieve card](https://developers.kaiten.ru/cards/retrieve-card)

### 8. Discovery целевых состояний
- Роль статуса — **колонки доски (columns)**, с machine-readable **`type`**:
  `1 - queue, 2 - in progress, 3 - done`. Плюс на уровне карточки — своё поле `state`
  (тот же 1/2/3 набор) и `condition` (1-live/2-archived).
- Список: `GET /api/latest/boards/{board_id}/columns`
  (`GET /columns/get-list-of-columns`). Поля колонки: `id`, `uid`, `title`, `type`, `sort_order`,
  `col_count`, `wip_limit`, `board_id`, `created`, `updated`, `subcolumns`, `pause_sla`.
  [Get list of columns](https://developers.kaiten.ru/columns/get-list-of-columns)
- Установка — `PATCH /cards/update-card` (`column_id`), либо смена card-level `state`.

### 9. Создание задачи
- `POST /api/latest/cards`. Обязательно: **`title`**, **`board_id`**. Опционально: `description`,
  `lane_id`, `column_id`, `due_date` (ISO 8601), `owner_id`/`responsible_id`, `size_text`,
  `properties` (кастомные поля в формате `id_{propertyId}: value`).
  [Create new card](https://developers.kaiten.ru/cards/create-new-card)

### 10. Rate limits
- **50 запросов/сек**; при превышении — **429**; есть заголовок `X-RateLimit-Remaining`
  (подтверждено вторичными источниками — точной официальной страницы с полным списком заголовков
  через прямой fetch получить не удалось, интерактивная документация не отдаёт статичный контент)
  — частично **НЕ НАЙДЕНО** (искал: "developers.kaiten.ru rate limit 429 X-RateLimit" —
  число "50 req/sec" встречается в нескольких независимых сторонних источниках/сводках по API,
  но официальную страницу с этим текстом зафетчить не вышло из-за SPA-рендеринга).

### 11. Подводные камни
- `type`-поле колонки использует **числовые enum-коды** (1/2/3), а не строки — нужно захардкодить
  маппинг при определении "done"-колонки.
- Kaiten и **kaiten.sh** — разные продукты; при поиске документации легко перепутать (см. предупреждение
  в начале раздела).
- Company-специфичный subdomain — `base_url` для каждого клиента индивидуален
  (`https://<company>.kaiten.ru`), нельзя захардкодить единый базовый URL для всех воркспейсов.
- Документация — SPA (интерактивный референс), многие страницы не отдают полный контент без
  выполнения запроса ("Try It!") — часть деталей (list-файлов, точная спецификация markdown в
  description) не подтверждена официальным текстом напрямую, только косвенно.

---

## 8. Weeek

> Официальная документация — `https://developers.weeek.net/` — построена как SPA (похоже на
> Readme.io-подобный движок), из-за чего прямой WebFetch отдаёт только общий shell/навигацию
> (одинаковый на любой странице), а не контент конкретного эндпоинта. Ниже — то, что удалось
> восстановить через официальный домен по поисковым сниппетам (индексированным Google) + через
> официальную PHP SDK на Packagist (издатель — сама Weeek), с пометками там, где подтверждение
> ограничено.

### 1. Base URL и версия API
- `https://api.weeek.net/public/v1/...` (версия `v1` в пути); задачи — под неймспейсом task manager:
  `https://api.weeek.net/public/v1/tm/tasks`.
  [Get tasks — Weeek Public API (сниппет)](https://developers.weeek.net/api-5330164)
- **Self-host**: официальной информации о self-hosted/on-premise версии Weeek **НЕ НАЙДЕНО**
  (искал: "Weeek self-hosted коробочная версия on-premise развертывание"); похоже на чисто SaaS-продукт.

### 2. Аутентификация
- `Authorization: Bearer <access_token>` — "All requests must contain bearer auth header".
  Токен создаётся в разделе API настроек воркспейса; **все запросы выполняются от имени того
  пользователя, кто создал токен** (важно для permission-модели).
  [Weeek Public API — authentication (сниппет)](https://developers.weeek.net/)
- Явного обязательного org/workspace-заголовка не обнаружено — токен уже привязан к воркспейсу.

### 3. Список задач + фильтр по времени изменения
- `GET https://api.weeek.net/public/v1/tm/tasks` — список задач.
- Точные query-параметры фильтрации (в т.ч. по `updatedAt`) и **модель пагинации**
  (page/limit или её отсутствие) — **НЕ НАЙДЕНО** (искал: "Weeek API tm/tasks GET query parameters
  projectId boardColumnId filter fromDate toDate updatedAt", "developers.weeek.net Get tasks
  response array fields example") — официальная страница эндпоинта не отдала контент через
  доступные инструменты (SPA), а сторонние источники не документируют эти параметры явно.
  Известно только, что задачи фильтруются по локации: `projectId`/`boardId`/`boardColumnId`
  (через body `locations` при создании — см. п.9), но не подтверждено, что те же поля работают
  как **query**-фильтры на GET.

### 4. Формат описания
- Поле **`description`** — формат (plain text/markdown/HTML) — **НЕ НАЙДЕНО** явного заявления в
  официальной доке (не удалось получить контент страницы напрямую). В теле создания задачи
  `description` передаётся как обычная строка.
  [Create task — Weeek Public API (сниппет с полями)](https://developers.weeek.net/api-5330163)

### 5. Подзадачи / чеклисты
- Подзадачи — через поле **`parentId`** при создании задачи (вложенность до **6 уровней**, могут
  использоваться и как чек-лист, и как полноценные задачи). В ответе на задачу есть массив
  **`subTasks`** (ID дочерних задач).
  Отдельного специализированного "checklist"-ресурса (отдельного от task-дерева) **не обнаружено**.
  [Weeek roadmap / task manager (вторичное подтверждение вложенности)](https://weeek.net/roadmap)

### 6. Вложения
- Отдельный эндпоинт **"Upload attachments"** существует (`developers.weeek.net/api-6504220`), но
  его точные параметры/URL/требования к авторизации при скачивании — **НЕ НАЙДЕНО** (страница не
  отдала контент через доступные инструменты).
  [Upload attachments — Weeek Public API (только заголовок найден)](https://developers.weeek.net/api-6504220)

### 7. Чтение по ключу / человекочитаемый ключ
- Ключ — числовой `id` задачи. `GET https://api.weeek.net/public/v1/tm/tasks/{id}`
  (`PUT`/`GET`/`DELETE` используют тот же путь с `{id}`). Человекочитаемого short-key
  (по аналогии с GitHub/Linear) не обнаружено.
  [Get one task info — Weeek Public API (сниппет)](https://developers.weeek.net/api-5330165)

### 8. Discovery целевых состояний
- На уровне **задачи** есть boolean-поле **`isCompleted`**, переключаемое отдельным эндпоинтом
  **"Complete task"** (`developers.weeek.net/api-5330167`, вероятно
  `PUT /tm/tasks/{id}/complete` или аналог — точный путь **НЕ НАЙДЕНО**, известно только название
  и факт существования из заголовка страницы/сниппета поиска).
- На уровне **доски** — задача имеет позицию через **`boardColumnId`** (может быть `null`, если не
  привязана к доске/колонке). Явного признака "какая колонка = done" (напр. `isDone`/`type` на самой
  колонке) в найденных материалах **НЕ НАЙДЕНО** — похоже, что "done"-семантика в Weeek определяется
  отдельным полем `isCompleted` на задаче, а не типом колонки, но список/discovery-эндпоинт для
  колонок доски (`board-columns`) с их полной схемой полей — **НЕ НАЙДЕНО**
  (искал: "Weeek API board-columns GET endpoint isCompleted OR type field").
  [Fields in WEEEK (общее, не API-специфичное)](https://weeek.net/help/workspace/tasks/fields)

### 9. Создание задачи
- `POST https://api.weeek.net/public/v1/tm/tasks`. Обязательное поле, подтверждённое из офиц. PHP
  SDK — **`title`** (`$client->taskManager->tasks->create(['title' => 'My task'])` работает с одним
  этим полем, т.е. остальные опциональны).
- Прочие поля тела (опциональные, по сниппетам офиц. доки): `description`, `day`, `parentId`,
  `userId`, **`locations`** (массив, каждый элемент — `{ projectId, boardId, boardColumnId }` —
  "куда создавать" в терминах доски), `projectId`, `boardColumnId`, `type` (enum: `action`/`meet`/
  `call`), `priority` (0-Low/1-Medium/2-High/3-Hold), `customFields`.
  [Create task — Weeek Public API (сниппет)](https://developers.weeek.net/api-5330163) ·
  [weeek/weeek-client-php (офиц. PHP SDK Weeek)](https://packagist.org/packages/weeek/weeek-client-php)

### 10. Rate limits
- **НЕ НАЙДЕНО** — числовые лимиты запросов, заголовки `X-RateLimit-*` и поведение при 429 для
  Weeek Public API в доступных источниках не обнаружены (искал: "Weeek API rate limit requests per
  second OR per minute 429").

### 11. Подводные камни
- Официальный сайт документации отдаёт контент только интерактивно (JS SPA) — большая часть точных
  сигнатур параметров в этом отчёте **не** подтверждена прямым чтением страницы, только через
  поисковые сниппеты Google и офиц. PHP SDK; **перед написанием адаптера обязательно вручную открыть
  developers.weeek.net в браузере** (или получить токен и curl'ом изучить реальные ответы) —
  это самый слабо задокументированный из восьми трекеров.
- "Все запросы выполняются от имени создателя токена" — важно для прав доступа сервисного
  интеграционного токена (не персонального пользователя).
- Задача может быть создана без привязки к доске/проекту вообще (только `title`) — "куда создавать"
  не является строго обязательным в отличие от остальных семи трекеров.

---

## Сводная таблица

| Доска | Auth-модель | Пагинация | Формат описания | Стиль API |
|---|---|---|---|---|
| **GitHub Issues** | `Authorization: Bearer <token>` + `X-GitHub-Api-Version` | `page`+`per_page` (max 100) + `Link`-заголовок (`rel=next`) | `body` — Markdown (GFM) | REST |
| **Trello** | query `?key=&token=` | `limit`; надёжного `since=updated_at` нет (только `dateLastActivity` + client-side фильтр) | `desc` — plain text | REST |
| **Linear** | `Authorization: <key>` (PAT, без Bearer) / `Authorization: Bearer <token>` (OAuth) | курсор `first`/`after`, `pageInfo.hasNextPage/endCursor` | `description` — Markdown | GraphQL |
| **ClickUp** | `Authorization: {pk_...}` (PAT, без Bearer) / `Authorization: Bearer {token}` (OAuth) | `page` (0-индекс), max 100/стр. | `description` (plain) / `include_markdown_description=true` → Markdown | REST |
| **Asana** | `Authorization: Bearer <token>` (PAT и OAuth одинаково) | `limit` (≤100) + opaque `offset` (`next_page.offset`) | `notes` (plain) / `html_notes` (HTML, opt-in) | REST |
| **Yandex Tracker** | `Authorization: OAuth <token>` или `Bearer <IAM-token>` + `X-Org-ID`/`X-Cloud-Org-ID` | `perPage` + `Link`/`X-Total-Count`, либо scroll (`scrollType`+`X-Scroll-Id`/`X-Scroll-Token`) | `description` — YFM (`markupType: "md"`) | REST |
| **Kaiten** | `Authorization: Bearer <token>`, company-subdomain base URL | `limit`(≤100)+`offset`, либо `version=2`+`start_position` | `description` — plain (markdown-спека не подтверждена) | REST |
| **Weeek** | `Authorization: Bearer <token>` | НЕ НАЙДЕНО (модель пагинации не подтверждена) | `description` — формат не подтверждён | REST |

---

## Пробелы (НЕ НАЙДЕНО)

- **Trello**: официальная точная модель пагинации для `GET /1/boards/{id}/cards` (page/before/limit
  за пределами `limit`) — доступна вероятно только через Postman collection/OpenAPI-спеку, не через
  prose-доку.
- **Linear**: официальный prose-текст, подтверждающий имя поля `children` для чтения sub-issues
  (подтверждено только по `parentId` в mutations и косвенно по схеме); официальное подтверждение,
  что `issue(id:)` принимает `identifier` (`ENG-123`) наравне с UUID.
- **Asana**: актуальный статус заголовка `Asana-Enable` (opt-in feature flags) в текущей версии API
  — не встретился в исследованных страницах.
- **ClickUp**: точная response-схема поля `statuses` в `GET /list/{list_id}` (имена полей status
  объекта) — интерактивная документация не отдаёт статичный пример; требуется ли авторизация при
  скачивании URL из `attachments`.
- **Yandex Tracker**: официальные числовые rate limits и связанные заголовки (`X-RateLimit-*`) —
  не найдены; официальная информация о self-host/on-premise развёртывании — не найдена (вероятно,
  отсутствует как опция).
- **Kaiten**: точный формат поля `description` (markdown/plain/HTML) на уровне официальной
  спецификации; явный GET-эндпоинт списка файлов карточки (card-files) отдельно от объекта карточки;
  требуется ли авторизация при скачивании файла; официальная страница-подтверждение "50 req/sec"
  лимита (число фигурирует только в сторонних сводках).
- **Weeek** (наибольшее число пробелов — сайт документации не отдаёт контент вне интерактивного
  режима):
  - точные query-параметры и модель пагинации `GET /tm/tasks` (в т.ч. фильтр по `updatedAt`);
  - формат поля `description`;
  - полная схема эндпоинта вложений (`api-6504220`) и требования к авторизации скачивания;
  - схема ресурса board-columns (список, поля, признак "done"-колонки, если он вообще существует
    отдельно от task-level `isCompleted`);
  - точный путь/метод "Complete task" (`api-5330167`);
  - числовые rate limits;
  - self-host/on-premise доступность.
  **Рекомендация**: перед реализацией адаптера Weeek открыть `developers.weeek.net` в браузере
  с реальным токеном воркспейса и снять точные сигнатуры запросов/ответов вручную (curl/Postman),
  либо запросить документацию у support@weeek.net.
