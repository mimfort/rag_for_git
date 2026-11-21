# Brief — PRI-274 + PRI-272: словарь причин недоступности источника контекста
https://ru.yougile.com/team/686c049c8af8/#PRI-274 · https://ru.yougile.com/team/686c049c8af8/#PRI-272

## Task
- **PRI-274** (ID-330): при ЖИВЫХ хранилищах исчерпание пула (`pg_pool_max_size=4`) классифицируется как `storage_unavailable` + remedy `reviewer start` — команда уже выполнена и не поможет. Настоящее лекарство — поднять размер пула или снизить параллелизм. Асимметрия: пачка задач причину сообщает (`warnings: "store: PoolTimeout: couldn't get a connection after 30.00 sec"`), контекст — нет.
- **PRI-272** (ID-328): при живых хранилищах и недоступном Voyage контекст выпотрошен молча — `cause: unknown`, `remedy: null`, а reason «задачи нет в сторе» указывает на доску вместо эмбеддера. `code`/`test_exemplars` = «(ничего не найдено)», `similar` = «(task search unavailable)».
- **Почему одним брифом:** обе отвечают на один вопрос — «какой класс причины получает этот сбой». Общий классификатор (`storage_health.py`), общая точка записи (`task_context.py`), общий потребитель (шаг 0a скилла), общие тесты. Раздельная реализация = два несогласованных решения о словаре `cause` и конфликты в четырёх файлах.
- **Критерии (объединённые):** пул и остановленные контейнеры отличимы по наблюдаемым полям; недоступный эмбеддер назван настоящей причиной, а не `unknown`; классификация решается ТИПОМ исключения без секретов в диагностике; неприменимое лекарство не советуется; шаг 0a предсказуем при новом cause с пустым remedy; всё закреплено тестами.

## Related work
- **PRI-277** — готовый механизм и прецедент решения: `cause_detail` уточняет причину ВНУТРИ класса, а сам `cause` намеренно не меняли, потому что шаг 0a ищет равенство `storage_unavailable` и тихо ослеп бы. Единственный источник класса и уместности лекарства — `classify_storage_failure`, общий для MCP и `reviewer check`.
- **PRI-268** — ввёл замыкание `_StorageState`, классификацию по типу исключения и 5-ключевой `gap()`. Ни пул, ни эмбеддер в его скоуп не входили — обе задачи закрывают именно эти дыры.
- **PRI-275** — тот же класс дефекта, что у PRI-272: fail-soft выше по стеку перехватывал исключение и крал сигнал у замыкания. Оставил намеренный тест-контракт под PRI-274 (см. Test exemplars).
- **PRI-276** — образец «провести причину, не ломая секцию»: отказ графа приходит в preflight полем `graph_error`, а не броском, плюс раздельное замыкание по бэкендам.
- **PRI-269** — приёмка 0.7.0, где оба дефекта измерены (Д-3 и Д-1) с числами; `eval/pri269_acceptance_report.md` — источник всех величин в тикетах.

(dropped 3: ID-322/ID-323 — постановка и приёмка того же поля, механизма не добавляют; ID-332 (PRI-276) уже учтён выше как linked.)

## Subsystems
- `reviewer/llm` — повторные попытки вызовов LLM при транзиентных ошибках: где сейчас живёт (не)распознавание сбоя Voyage.
- `reviewer/tasks` — синк и индексация задач: 5 из 11 call-сайтов `is_storage_unavailable`, здесь же `retry_required`.
- `reviewer/policy` — лимиты контекста retrieval-тулов: секции, которые обедняются при мёртвом эмбеддере.
- `tests/entrypoints` — тесты CLI: второй потребитель классификатора (`reviewer check`).

(dropped 4: tests/tasks, tests/retrieval, tests/web, plugin/hooks — соседние по поиску, механизма задачи не задают.)

## Relevant code
- `reviewer/storage_health.py:57-68` — `is_storage_unavailable`; докстринг **прямо заявляет**, что `PoolTimeout` покрыт намеренно («одна проверка покрывает и таймаут пула, и обрыв соединения»). PRI-274 меняет объявленный инвариант — докстринг обязан меняться вместе с кодом.
- `reviewer/storage_health.py:26-31` — `CAUSE_STORAGE_UNAVAILABLE`/`CAUSE_UNKNOWN`/`REMEDY_START`/`BACKEND_*`: словарь, который расширяет PRI-272.
- `reviewer/storage_health.py:122-136` — `DETAIL_AUTH_FAILED`/`DETAIL_MISSING_DATABASE` + `_DETAIL_PATTERNS`: готовая точка расширения под пул. Порядок паттернов значим (конкретнее — раньше).
- `reviewer/storage_health.py:192-215` — `classify_storage_failure`: единственный источник и класса, и уместности `reviewer start`; ветка `not is_storage_unavailable → пустой вердикт` — рабочая, ею чинится Neo4j `AuthError`.
- `reviewer/storage_health.py:71-85` — `storage_backend`: решает, кого пропускать в замыкании; пара к предикату, покрытие общее.
- `reviewer/mcp/task_context.py:41-50` — `gap()`: 5-ключевой контракт `{section, reason, cause, cause_detail, remedy}`, по которому ветвится клиент.
- `reviewer/mcp/task_context.py:53-83` — `_StorageState`: замыкание раздельно по бэкендам, `mark` кеширует диагноз.
- `reviewer/mcp/task_context.py:86-115` — `_storage_gap` + `_reason_with_detail`: `DETAIL_REASONS` замещает общую прозу, отрывок дополняет.
- `reviewer/mcp/task_context.py:33-38` — `DETAIL_REASONS`: формулировки классов живут здесь, не в `storage_health`.
- `reviewer/mcp/task_context.py:118-143` — `_safe`: **единственное место**, где исключение превращается в gap. Всё, что не доехало сюда, для пользователя не существует.
- `reviewer/mcp/task_context.py:239-243` — «задачи нет в сторе»: формулировка, которую PRI-272 п.3 требует заменить.
- `reviewer/tasks/service.py:379-385` — `search_hits`: `except Exception → None`, гасит APIError эмбеддера. **Барьер 1 из 3.**
- `reviewer/tasks/service.py:392` — `render_hits`: `None → "(task search unavailable)"`; `None` vs `[]` различаются намеренно.
- `reviewer/mcp/service.py:1813-1819` — `search_codebase`: `except Exception → "(ничего не найдено)"`. **Барьер 2.**
- `reviewer/mcp/service.py:1839-1848` — `_search_codebase_multi` (секции `code`/`test_exemplars`): то же. **Барьер 3.**
- `reviewer/index/embeddings.py:113-118` — `_call` → `with_voyage_retry`: точка, где рождается APIError Voyage.
- `reviewer/index/_retry.py:6-15` — `_is_rate_limit` судит по **имени типа и тексту**, а не по типу: контр-пример к требуемому стилю, трогать при расширении осторожно.
- `reviewer/config/fetch_errors.py:24-40` — `classify_fetch_error`: канонический прецедент классификации по форме исключения.
- `reviewer/entrypoints/cli.py:890,922` — `reviewer check`: второй потребитель `classify_storage_failure`, обязан согласоваться.
- `plugin/skills/solve-task/references/preflight.md:48-80` — шаг 0a: ветвится по `cause == storage_unavailable`, читает `cause_detail` и `remedy`. Новый `cause` без правки этого файла клиентом не увидится.
- **Blast radius `is_storage_unavailable`** (граф + grep): 11 call-сайтов в 4 файлах — `tasks/service.py:195,238,261,343,429`, `mcp/service.py:516,1671`, `mcp/task_context.py:137,160`. Изменение предиката задевает `retry_required` пачки задач и strict-проброс PRI-275.

(dropped 12: `services/gc.py`, `metrics/brief_quality/classify.py`, `tasks/boards/base.py`, `graph/family.py`, `bugreport/*`, `policy/context_limits.py`, `install.py`, `graph/summaries.py`, `config/committed.py`, `tasks/store.py`, `tasks/boards/weeek.py`, `services/status.py` — подняты ретривом как соседи по подсистеме, ни один не редактируется и не копируется.)

## Test exemplars
- `tests/mcp/test_service.py:1160-1186` — `test_task_context_deps_preflight_raises_and_stops_after_one_store_call`: бросает **`psycopg_pool.PoolTimeout`** и докстрингом объявляет контракт — «если PRI-274 выведет PoolTimeout из `is_storage_unavailable`, этот тест обязан покраснеть». Прямой сигнал развилки ниже.
- `tests/test_storage_health.py:16-17` — `test_operational_error_is_storage_unavailable`: базовое утверждение предиката, которое правка затрагивает первым.
- `tests/tasks/test_service_batch.py` — `test_storage_down_mid_hash_phase_still_blocks_voyage_call`: **смешанный** стор (первая задача доходит до `to_embed`, падает вторая) — тот самый паттерн против guard-теста, зелёного по построению.
- `tests/mcp/test_prepare_task_context.py` — состав секций и `gaps` целиком: сюда ложатся оба новых случая.
- `tests/mcp/test_session_store.py:53-66` — `test_live_keys_raises_when_db_unavailable`: образец «источник недоступен → бросок, а не тишина».
- `tests/entrypoints/test_infra_commands.py` — `test_check_stays_silent_for_remote_storages`: как проверяется уместность remedy на стороне CLI.
- `tests/skills/test_preflight_guardrail.py:20-26` — guard на текст скилла: правка `preflight.md` без него разъедется с сервером.

(dropped 5: `tests/skills/test_create_task_skill.py`, `tests/bugreport/test_render_publish.py`, `tests/config/test_review_branches.py`, `tests/retrieval/test_multiquery.py`, `tests/test_infrastructure_policy.py` — соседи по поиску, паттерна для этих задач не дают.)

## Constraints / open questions
- **Главная развилка PRI-274.** Буквальный текст п.1 («отделить `PoolTimeout` проверкой isinstance ДО общего `OperationalError`») читается двояко: **(A)** вывести `PoolTimeout` из `is_storage_unavailable` — тогда замыкание `_StorageState` перестанет его ловить и вернутся 8 таймаутов по 30 с вместо одного, то есть прямая регрессия PRI-268/275/276; **(B)** оставить в предикате (замыкание работает), но выдать `cause_detail: pool_exhausted` + `remedy: null` — механизм PRI-277 один-в-один. Критерии приёмки PRI-274 (отличимость + отсутствие неверного лекарства) выполняются вариантом (B) без регрессии. Решить явно на брейншторме; тест `test_task_context_deps_preflight_...` — индикатор того, какой вариант выбран.
- **Развилка PRI-272 противоположна по знаку.** Здесь `cause_detail` внутри `storage_unavailable` был бы ложью (Voyage не хранилище, `reviewer start` не лечит), значит нужен новый `cause` — а это меняет контракт клиента, который сегодня ветвится на равенство `storage_unavailable`. Прецедент PRI-277 говорит «не трогай cause», механика задачи говорит «придётся». Правка `preflight.md` + guard-теста обязательна, иначе новый cause не дойдёт до пользователя.
- **PRI-272 — это не только классификатор.** Три fail-soft барьера гасят APIError эмбеддера ДО `_safe` (`tasks/service.py:383`, `mcp/service.py:1817`, `mcp/service.py:1845`). Пока причина не пройдёт через них, никакой новый `cause` в `gaps` не появится — расширение словаря в одиночку задачу не закроет. Это ровно тот же дефект, который чинила PRI-275, только в трёх местах.
- **Классификация Voyage по типу возможна:** `voyageai.error` даёт `APIError`, `AuthenticationError`, `APIConnectionError`, `RateLimitError`, `ServerError`, `ServiceUnavailableError`, `Timeout`, `InvalidRequestError`. Отделить «эмбеддер недоступен» от «rate limit» (который штатно ретраится) нужно по типу — `_retry.py` судит по имени и тексту и образцом служить не должен.
- **Воспроизведение PRI-274 захватывает весь пул** (`pg_pool_max_size = 4`, `reviewer/config/settings.py:78`) и положит любую параллельную сессию reviewer. Замер делать в одиночку, не одновременно со второй дорожкой (PRI-273).
- **Воспроизведение PRI-272 требует недоступного Voyage.** В замере PRI-269 это давал датацентровый IP (403 от посредника до аутентификации). Локально нужен управляемый способ (подмена base URL / блокировка), иначе критерий не проверить.
- `[existing_artifacts]` — не найдено: briefs/specs/plans по обоим ключам пусты, `.superpowers/sdd` тоже.
- **GitHub API отдаёт 401** (`reviewer check` на 2026-09-05) — токен протух. Индексации не мешает, но ревью PR и бэклинк `finish_task` работать не будут, пока не обновлён.
- **Правка `plugin/skills/**` меняет codex payload-digest** → нужен прогон `update_codex_plugin_manifest.py`, иначе install-тесты красные.
- **Контекст свежий:** индекс переиндексирован на `ebfa663` (drift 0, chunks 7877, граф SCIP 8128 узлов / 19867 рёбер), сводки подсистем прогреты (40), `gaps` предполёта пуст.
- **PRI-275 решать не нужно** — смержена (PR #232, `ebfa663`), фикс `_repo_clone_path(strict=True)` на месте; на доске осталась незакрытой, закрывается через `rag-reviewer:finish-task`.
- **PRI-273 идёт второй дорожкой** в отдельном worktree: `_record_history` (`mcp/service.py:3275`) с этими задачами по коду не пересекается.
Собран на: premium (Opus 5), сборка: inline
