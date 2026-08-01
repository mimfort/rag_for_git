# PRI-224 — декомпозиция задачи в нативные подзадачи

**Статус:** дизайн утверждён по секциям 2026-08-01
**Исходный бриф:** `docs/superpowers/briefs/2026-08-01-PRI-224-decompose-task-native-subtasks.md`

## Цель

Добавить skill `rag-reviewer:decompose-task`, который превращает одну существующую задачу в
подтверждённый пользователем набор автономных задач и записывает их как нативные подзадачи
YouGile. Все credentials и многошаговые board-write остаются внутри reviewer MCP.

Операция обязана переживать restart MCP, продолжать partial batch с тем же
`idempotency_key`, не создавать дубликаты и сохранять уже существующие связи родителя.

## Принятые решения

- Первый и единственный обязательный provider — YouGile.
- Другие providers получают явный `unsupported` до первого board-write.
- Ребёнок наследует текущую колонку родителя.
- Один batch содержит от 1 до 20 детей.
- Каждый ребёнок передаётся как полный `TaskDoc`: `title`, `problem`, `steps`, `criteria`,
  `context`.
- Idempotency record хранится без TTL и переживает restart/deploy; активные операции
  сериализуются parent-scoped Postgres advisory lock.
- Повтор partial operation продолжает незавершённые шаги, а не возвращает неизменный snapshot.
- До одного явного подтверждения пользователя нет ни одного board-write.
- Links должны быть доступны и в Postgres task store, и в Neo4j task graph.

## Не входит в scope

- Поддержка native subtasks в YouTrack, Jira и остальных adapters.
- Рекурсивная декомпозиция и вложенность глубже parent -> child.
- Rollback или удаление уже созданных карточек при partial failure.
- Гарантия сохранения subtask UUID, добавленного вручную в UI конкурентно между последним
  parent GET и PUT: YouGile API не предоставляет ETag/CAS для этого поля.
- Новая веб-форма или изменение UI reviewer.
- Автоматический выбор decomposition без preview пользователя.
- Full-board force renormalization существующих задач.

## Архитектура

### MCP schema

Новая входная модель `SubtaskIn` содержит:

```text
title: str
problem: str
steps: list[str]
criteria: list[str]
context: str | null
```

Новый session-less MCP tool:

```text
create_subtasks(
  parent_key,
  subtasks,
  idempotency_key,
  board_type,
  project,
  provider_options,
) -> SubtaskBatchResult
```

FastMCP и сервис валидируют непустые `parent_key`/`idempotency_key`, непустые `title`/`problem`,
хотя бы один непустой step и criterion у каждого ребёнка, список размером 1..20 и
JSON-совместимые generic provider options. Canonical markdown каждого ребёнка собирается
server-side существующим `TaskDoc`/`render_markdown`; клиент не присылает HTML.

### Optional provider capability

`BoardProviderSpec` получает immutable capability set. Для этой задачи определяется capability
`native_subtasks`. Основной `TaskBoardProvider` не расширяется, чтобы десять неподдерживаемых
adapters не получили фиктивные stubs.

Отдельный `NativeSubtaskProvider` задаёт три provider-примитива:

- reconcile карточек во всех колонках зафиксированной source board по набору markers;
- создать одного ребёнка в колонке родителя и вернуть UUID/canonical key/URL;
- заменить native `subtasks` родителя переданным union UUID.

Registry проверяет capability-specific методы только у spec, объявившего `native_subtasks`.
Ошибочно объявленный capability делает provider invalid при создании. `get_board_targets`
добавляет registry-owned `capabilities`, чтобы skill мог остановиться до preview; server-side
`create_subtasks` повторяет проверку как authoritative guard до write.

### Durable operation store

Новый `SubtaskOperationStore` использует Postgres и отдельную таблицу:

```text
subtask_operations
  idempotency_key     text PRIMARY KEY
  board_type          text
  parent_input_key    text
  parent_task_id      text
  source_board_id     text
  source_column_id    text
  request_hash        text
  request_payload     jsonb
  state               jsonb
  status              text
  created_at          timestamptz
  updated_at          timestamptz
```

Operation rows не удаляются автоматически, а `idempotency_key` глобально уникален в deployment.
Store читает row по key до provider resolution: completed operation возвращает сохранённый
результат без provider calls; совпавший key с другим request hash получает `conflict`.

Перед любой новой или resumed записью store удерживает dedicated Postgres connection с
session-level advisory lock, вычисленным из `(board_type, parent_task_id)`. Lock сериализует все
idempotency keys одного parent, поэтому два batch не могут записать competing UUID unions.
`pg_try_advisory_lock` не ждёт: занятый parent возвращает `in_progress`, `retryable=true`.
Process crash закрывает connection и освобождает lock; следующий вызов продолжает durable row.
Перед каждым board-write service проверяет, что lock connection жива; потеря lock останавливает
операцию до следующей попытки.

Operation status имеет значения `running`, `partial`, `board_complete`, `complete`. Обычный возврат `partial`
сохраняет checkpoint и освобождает advisory lock, поэтому пользователь может повторить вызов
сразу. `running` без удерживаемого lock означает прерванный процесс и безопасно resume-ится.
`board_complete` означает, что все native links записаны, но targeted write-through ещё не
подтверждён; только `complete` можно replay-ить без provider calls.

`request_hash` считается из переданных board type/project/parent key/options и canonical JSON
упорядоченного списка всех полей детей. Перестановка детей считается другим payload. Task text
допустимо хранить в operation row:
это те же несекретные данные, которые будут записаны в configured board. Ответы и ошибки всё
равно проходят существующую secret sanitization.

### Resumable orchestration

Новый `SubtaskService` содержит state machine и зависит от `SubtaskOperationStore`, переданного
`NativeSubtaskProvider` и injected idempotent write-through callback. Callback принимает parent и
confirmed children, полностью нормализует/индексирует их и возвращает success/warnings. Provider
resolution, credentials, callback assembly и response sanitization остаются в `MCPReviewService`.
Service вызывает callback до выхода из parent advisory-lock context и только затем ставит
`complete`, поэтому между board phase и indexing нет недолговечного orchestration gap.

State каждого input item хранит index, input hash, marker, phase, title, board UUID, canonical
key, aliases, URL и безопасные warnings. Phases: `pending`, `in_flight`, `created`, `attached`.

Если operation store недоступен, вызов завершается fail-closed до board-write. Это осознанное
исключение из общего fail-open стиля: без durable checkpoint невозможно выполнить гарантию
no-duplicates.

## Data Flow

### Skill до подтверждения

1. Один раз разрешить `task_board` из `.review.yml` с deploy fallback по существующему pattern.
2. Прочитать parent store-first через `get_task(parent_key, project)`; при miss выполнить один
   incremental `sync_board` и повторить чтение.
3. Получить `get_board_targets` и проверить `native_subtasks` capability. Unsupported завершает
   flow без draft/write.
4. Собрать `get_task_context`, похожие задачи и relevant code через session-less retrieval tools.
5. Сформировать 1..20 автономных `SubtaskIn`: каждый должен иметь собственную цель, шаги и
   проверяемые acceptance criteria.
6. Сгенерировать один opaque UUID idempotency key средствами клиентского runtime и сохранить его
   verbatim для всех повторов этого preview.
7. Показать provider, parent, idempotency key и полное canonical body каждого ребёнка.
8. Выполнить ровно один `create_subtasks` только после явного подтверждения всего preview.

Редактирование preview пользователем создаёт новый payload и новый idempotency key до write.
После partial result key не меняется.

### Server-side batch

1. Валидировать request и вычислить request hash без provider calls.
2. Прочитать operation по global idempotency key. Completed row возвращается
   сразу; payload mismatch завершается conflict.
3. Для новой/resumed операции разрешить provider и проверить capability до любого provider write.
4. Для новой операции прочитать parent и получить stable task UUID,
   board ID, текущий column ID и `subtask_ids` через `RawTask`/`provider_data`.
5. Получить parent-scoped advisory lock, повторно проверить operation row под lock и создать либо
   resume-ить state. Первая попытка фиксирует source board/column для всего batch.
6. Один раз reconcile-ить все markers без confirmed UUID, просканировав все колонки source board.
7. Перед POST durable-checkpoint-ить item как `in_flight` с marker, затем проверить владение
   advisory lock. Ребёнок получает
   зафиксированный source column ID родителя, server-rendered HTML description и marker.
8. Успешный POST обязан вернуть UUID. Provider best-effort выполняет GET для canonical key/URL,
   но отсутствие enrichment не отменяет create: service немедленно checkpoint-ит UUID как
   `created` с fallback key и warning.
9. После цикла перечитать parent непосредственно перед update, сохранить порядок существующих
   UUID и добавить отсутствующие confirmed UUID в input order.
10. Если parent уже содержит весь ожидаемый union, без PUT checkpoint-ить known children как
    `attached`. Иначе выполнить один parent PUT, перечитать parent и объявить `attached` только
    после проверки ожидаемого union.
11. Сохранить operation как `board_complete` и выполнить targeted write-through parent + confirmed
    children, пока parent advisory lock удерживается.
12. Только успешное обновление Postgres links и Neo4j edges переводит operation в `complete`.
    При сбое persisted status остаётся `board_complete`, наружу возвращается `status=partial`,
    `category=reindex_pending`; повтор не пишет в board и повторяет только normalization/indexing.

### Deterministic marker и ambiguous writes

Marker строится как hex SHA-256 от board type, parent UUID, idempotency key, item index и input
hash:

```text
reviewer-subtask:<hex>
```

YouGile добавляет marker после `md_to_html` как небольшой plain-text metadata footer внутри HTML.
Технический token виден в UI, зато не зависит от недокументированного сохранения HTML comments.
`normalize_yougile` удаляет только exact `reviewer-subtask:<64 hex>` перед `html_to_md`, поэтому
marker не попадает в normalized description. Reconcile ищет exact token среди всех колонок source
board, поэтому sanitization HTML и перемещение parent после partial batch не создают duplicate
child.

Если POST мог выполниться, но response потерян, item остаётся `in_flight`; текущий invocation не
повторяет POST. После освобождения advisory lock следующий вызов сначала reconcile-ит marker.
Если marker ещё не виден, вызов возвращает retryable partial без POST: client timeout не доказывает,
что YouGile не закоммитит первый request позднее. Один и тот же idempotency key никогда
автоматически не переотправляет `in_flight` item. Повторные вызовы продолжают reconcile и все
остальные безопасные шаги; item без marker сохраняет persisted phase `in_flight`, а наружу
отображается в result bucket `pending` с `manual_required=true` до eventual visibility или ручного
решения пользователя. Он не возвращается в обычный `pending -> POST` flow. Provider contract test
доказывает поиск marker независимо от HTML-обёртки.

Это осознанный safety-over-liveness выбор при отсутствии board-side idempotency/unique constraint:
гарантия no-duplicates действует для повторов с тем же key, но ambiguous POST без marker может
потребовать ручной проверки доски. Новый key после такого отказа считается новой операцией и не
входит в гарантию старого key.

## Result Contract

`SubtaskBatchResult` содержит:

```text
status: ok | partial | error
board_type
parent_key
idempotency_key
resumed: bool
created: list[child result]
attached: list[child result]
unattached: list[child result]
pending: list[input result]
warnings: list[str]
category: optional error category
retryable: optional bool
```

`created` кумулятивно перечисляет всех детей с подтверждённой board identity, включая recovered
по marker. `attached` — подтверждённые дети, входящие в записанный parent union. `unattached` —
подтверждённые карточки, которые parent PUT ещё не связал. `pending` — inputs без подтверждённой
board identity; такой result item дополнительно несёт persisted phase и `manual_required`, чтобы
`in_flight` нельзя было принять за ещё не начатый `pending`.

Child result содержит `index`, `title`, canonical `key` (`idTaskCommon`), `aliases`
(`idTaskProject`, если отличается), transport `board_id` (UUID) и `url`. Если YouGile не вернул
canonical code, `key` временно деградирует до UUID с warning; такой ребёнок остаётся пригодным для
attachment, но verification отмечает отсутствие canonical identity.

`error` означает, что операция остановилась до возможного write: validation, unsupported,
configuration, unavailable ledger, conflict. После первого возможного side effect операционные
ошибки дают `partial` и состояние из ledger. Process crash может не вернуть response; повтор
восстанавливает состояние по ledger + markers.

`ok` возвращается только для `complete`. Persisted `board_complete` с неуспешным write-through
возвращается как `partial`, `category=reindex_pending`, с confirmed `attached` children,
`reindexed=false` и retryable warning.

Занятый parent advisory lock возвращает `category=in_progress`, `retryable=true`. Payload mismatch возвращает
`category=conflict`, `retryable=false`. Секреты не входят в marker, operation identity, result или
логи.

## YouGile Normalization

Текущий `normalize` передаёт `uuid -> "ID-N:title"`, а `normalize_yougile` использует UUID как
`links[].key`. Это заменяется на richer mapping `uuid -> {key, title}`. Canonical `idTaskCommon`
становится link key; UUID остаётся только transport identity. При недоступном child GET fallback
остаётся UUID с warning, не потеря всей parent task.

Обычный `sync_board` остаётся обязательным пользовательским post-write шагом, но correctness не
зависит от board timestamp/watermark: targeted write-through индексирует parent и confirmed
children сразу после batch. Force renormalization всей доски не требуется.

## Links в Store и Graph

В `tasks` добавляется `links jsonb NOT NULL DEFAULT '[]'`. `TaskRow`, full upsert, read и
`TaskService.get_task` сохраняют/возвращают links. Links не входят в `build_task_text` и
`content_hash`, поэтому миграция не вызывает переэмбеддинг.

Full `index_task/index_batch` рассматривает явно переданный `links` как authoritative snapshot.
Links записываются отдельным store metadata update независимо от решения `embedded/meta_only`:
parent description обычно не меняется при attachment, поэтому равный `content_hash` не должен
пропустить новый snapshot. Если поле отсутствует у legacy/partial caller, существующие links не
очищаются. `normalize_meta` и `refresh_meta_batch` links не меняют.

`TaskGraph.replace_links` в одной Neo4j transaction удаляет исходящие `TASK_LINK` parent и MERGE-ит
текущий snapshot. Метод вызывается и для явно пустого списка, чтобы удалить stale edges. Это
заменяет старый UUID stub canonical `ID-N`, а не оставляет обе связи. Incoming links других задач
не затрагиваются.

## Post-write Sync и Verification

После `board_complete` injected callback от `MCPReviewService` выполняет targeted write-through
внутри того же `SubtaskService.run` и удерживаемого parent lock:
перечитывает parent и confirmed children, полностью нормализует их и передаёт одним
`TaskService.index_batch`. Это обновляет store/graph links даже при неизменном parent
`content_hash` и независимо от watermark. Result получает `reindexed` и write-through warnings.
Успех checkpoint-ит operation как `complete`; crash после фактической индексации, но до checkpoint,
приведёт лишь к безопасному повтору idempotent write-through.

Затем skill вызывает scoped incremental `sync_board` с тем же resolved board config и проверяет:

- `get_task(parent_key, project).links` содержит canonical keys attached children;
- `get_task_context(parent_key, project)` показывает те же TASK_LINK edges;
- created children доступны через `get_task` по returned canonical key/alias.

Verification gaps не меняют board-write result и не запускают автоматический повтор с новым key.
Skill выводит их отдельным списком. При `partial` он предлагает повторить тот же payload и key.

## Error Handling

- Unsupported capability, malformed input, batch >20 и idempotency conflict — no-write errors.
- Provider/transport messages проходят `sanitize_provider_text/payload` с resolved secrets.
- Child create errors не отменяют confirmed siblings; результат становится partial.
- Успешный POST с UUID и неуспешный enrichment GET — confirmed child с UUID fallback/warning,
  а не `unknown`; attachment может продолжиться.
- Parent read failure до creates — error; повтор безопасен.
- Parent reread/PUT failure после creates — children остаются `unattached`; повтор не создаёт их.
- Existing parent UUID сохраняются; union дедупится без изменения исходного порядка.
- Если union уже записан, parent PUT не выполняется, children checkpoint-ятся как `attached`, а
  operation переходит в `board_complete`.
- Все reviewer-процессы сериализуют subtask batch по parent UUID. Гарантия сохранения existing
  links относится к UUID из немедленного pre-PUT snapshot; внешний UI YouGile не даёт ETag/CAS,
  поэтому конкурентное ручное изменение явно вне atomicity guarantee. Post-PUT read не объявляет
  успех без ожидаемого union.
- Rollback отсутствует намеренно: удаление confirmed child могло бы потерять пользовательские
  изменения между попытками.

## Skill UX

Новый `plugin/skills/decompose-task/SKILL.md` имеет `name: decompose-task` и отвечает по-русски.
Skill остаётся provider-agnostic: он проверяет generic capability и не ветвится по строке
`yougile`. YouGile-specific поведение живёт только server-side.

Preview — единственная confirmation boundary. Он показывает полный список и не выполняет
`create_task` по одному. Confirm разрешает ровно один batch call. Decline/редактирование не пишет
на доску. Partial retry использует тот же key и неизменный payload; иначе server вернёт conflict.

## Testing

### Pure/service unit tests

- Validation 1..20, nonblank problem/title, nonempty steps/criteria и canonical request hash.
- Fresh success и checkpoint после каждого ребёнка.
- Completed replay без provider calls.
- Same key/different payload conflict до write.
- Parent advisory lock, competing idempotency keys и resume после simulated connection close.
- Pre-POST in-flight checkpoint; ambiguous POST никогда не переотправляется автоматически,
  repeated reconcile даёт один POST суммарно.
- Child failure, parent PUT failure, partial result и resume only missing steps.
- Stable existing+new UUID union, дедуп и no-op PUT -> attached/board_complete.
- Crash между board_complete/write-through/complete повторяет только idempotent indexing.

### Store tests

- Schema creation/migration, operation CAS, parent advisory lock и checkpoint persistence.
- Новый store instance читает operation после simulated restart и возвращает completed result
  без provider calls.
- Concurrent claimant того же parent не получает advisory lock, включая другой idempotency key.
- Postgres integration подтверждает transaction/concurrency invariants.

### YouGile provider tests

- Child POST наследует parent `columnId` и содержит canonical HTML + unique plain-text marker.
- Second GET resolves UUID, canonical key и URL.
- Reconcile находит exact marker в любой колонке source board.
- Ambiguous POST, перенос parent и recovery marker из исходной колонки/source board.
- Parent перечитывается после create cycle.
- PUT сохраняет старые UUID и добавляет новые без дублей.
- Mock transport моделирует выполненный POST с потерянным response.
- Ambiguous transport без созданной карточки остаётся pending и не делает второй POST.
- Raw description с любой HTML-обёрткой сохраняет marker text, normalized markdown marker не содержит.

### Contract/MCP tests

- Capability-specific runtime validation; providers без capability не меняются.
- Unsupported provider не вызывает write method.
- FastMCP schema и generic metadata/options.
- Provider lifecycle всегда закрывается.
- Result/error sanitization не пропускает credentials.

### Store/graph tests

- YouGile subtask link использует canonical `ID-N`.
- Links round-trip через `TaskStore` и `get_task`.
- Full sync заменяет stale UUID edge canonical edge.
- Meta-refresh не очищает links.

### Skill/plugin tests

- Store-first parent + one sync/retry on miss.
- Context/code retrieval до draft.
- Full preview и explicit confirmation до единственного write.
- Same key retry после partial.
- Scoped sync и три post-write verification checks.
- Frontmatter basename, dynamic payload, README/README.ru/AGENTS references.
- Codex manifest пересобран после plugin payload change.

Финальная проверка реализации: точечные suites, полный `.venv/bin/pytest -q` и
`.venv/bin/ruff check .`. Unit и обычные integration tests не используют реальные board
credentials или внешнюю сеть.

## Документация

README/README.ru получают reference нового skill и MCP tool. AGENTS.md перечисляет
`rag-reviewer:decompose-task`. MCP tool count обновляется во всех guarded местах. После изменения
`plugin/` запускается `scripts/update_codex_plugin_manifest.py`, затем manifest guard.
