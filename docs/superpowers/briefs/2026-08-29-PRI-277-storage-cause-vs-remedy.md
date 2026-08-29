# Brief — PRI-277 Неверные креды и несуществующая БД неотличимы от остановленных контейнеров
https://ru.yougile.com/team/686c049c8af8/#PRI-277

## Task

**ID-333 (PRI-277)**. `storage_remedy()` (`reviewer/storage_health.py:60-64`) советует `reviewer start`
только по признаку loopback-эндпоинта, не проверяя, применимо ли лекарство. Замер приёмки PRI-269
(критерий 8, дефект Д-6, `eval/pri269_acceptance_report.md`): неверный пароль, несуществующая БД и
остановленные контейнеры при живых контейнерах дают побайтово одинаковый ответ (`is_storage_unavailable
True`, 8 gaps, `cause: storage_unavailable`, `remedy: "reviewer start"`, ~60 с) — пользователю с рабочим
Postgres советуют команду, которая уже выполнена.

Что сделать: (1) различить auth-сбой и отсутствующую БД по тексту исключения — SQLSTATE недоступен
(`e.sqlstate`/`e.diag.sqlstate` всегда `None` при сбое установления соединения, доказано замером);
(2) вымарать хост/порт/имя пользователя из текста, оставив класс причины, по образцу
`config/fetch_errors.py`; (3) не предлагать `reviewer start`, когда порт открыт и отвечает настоящий
Postgres; (4) сохранить `cause: storage_unavailable` — правка касается `remedy`/формулировки причины,
не классификации; (5) покрыть тестом оба случая + отсутствие секретов в диагностике.

Критерии приёмки: (1) неверный пароль и несуществующая БД отличимы от остановленных контейнеров по
наблюдаемым полям ответа; (2) `reviewer start` не советуется при живых контейнерах; (3) ни пароль, ни
полный DSN в диагностику не попадают; (4) поведение закреплено тестом.

## Related work
- **ID-330** — «Исчерпание пула соединений выдаётся за недоступность хранилища (PoolTimeout не
  отделён от OperationalError)» — та же поверхность (`is_storage_unavailable`/`storage_remedy` в
  `storage_health.py`), сиблинг-задача про добавление гранулярности в тот же модуль; дизайн этой
  задачи не должен закрывать путь к отдельной ветке для `PoolTimeout`.
- (dropped 6: ID-328 — недоступность эмбеддера, другой сабсистем; ID-322/ID-323 — таймлайн-сигнал
  solve-task про сам факт лежащей инфраструктуры, не про текст remedy; ID-332 — молчание Neo4j 162 с,
  другой класс; ID-331 — двойной таймаут пула в preflight; ID-329 — `publish_review` не пишет историю —
  фон, ни один не информирует текстовую классификацию auth/missing-db)

## Subsystems
- reviewer/config — Settings + pg/neo4j-коннекты; здесь же лежит `config/fetch_errors.py`, названный
  в задаче образцом фильтра.
- reviewer/entrypoints — CLI (`check`) дублирует ровно тот же класс решения независимо от
  `storage_health.storage_remedy`.
- tests/config — дом `tests/config/test_fetch_errors.py`, эталонный паттерн теста «секрет не в выдаче».
- (dropped 5: reviewer/web, tests/web, reviewer/services, reviewer, tests/install — веб-админка,
  ревью PR, установка; ни один не касается `storage_health`/`task_context`/`cli.check`)

## Relevant code

Целевые модули (обязательные, добраны оркестратором, тела читаны целиком):
- `reviewer/storage_health.py:1-14` — докстринг модуля прямо обосновывает «по типу, не по тексту»
  тем, что «в тексте psycopg.OperationalError живёт DSN с паролем». Замер PRI-269 это на данном
  классе ошибок опровергает (пароль в `str(exc)` отсутствует, хост/порт/юзер/имя БД — присутствуют).
  Это противоречие декларации и факта — не решать самому, см. Constraints.
- `reviewer/storage_health.py:30-43` — `is_loopback_endpoint`: `urlsplit(value).hostname` с фолбэком
  на regex `host=([^\s]+)` для psycopg keyword-DSN. Даёт готовый способ достать host/port/user из
  сконфигурированного DSN для вымарывания (криитерий 2 задачи).
- `reviewer/storage_health.py:46-57` — `is_storage_unavailable`: классифицирует по `isinstance`
  (`psycopg.OperationalError` вкл. `PoolTimeout`, neo4j `ServiceUnavailable`/`SessionExpired`).
  Не трогать — `cause` остаётся верным по условию задачи.
- `reviewer/storage_health.py:60-64` — `storage_remedy(*endpoints)` — точка правки: сигнатура берёт
  только эндпоинты, не эксепшн. Единственный вызывающий — `task_context.py:126`.
- `reviewer/mcp/task_context.py:42-52,55-62,65-85,116-129,132-188` — потребитель. **Ключевая находка
  для blast radius**: `state = _StorageState(_remedy(deps))` вызывается на строке 138, ДО первого
  реального обращения к хранилищу — `_remedy` знает только сконфигурированные `deps.storage_endpoints()`
  (loopback да/нет), эксепшна там ещё нет. Сам эксепшн ловится позже, внутри `_safe` (строки 76-85),
  но туда не передаётся — `_storage_gap` читает уже готовый `state.remedy`, зафиксированный на старте.
  Текстовая классификация по эксепшну технически недостижима без переноса вычисления remedy из
  eager-вызова в `build_task_context:138` в момент фактического `except Exception as exc` внутри
  `_safe` (и переиспользования результата первой ошибки для последующих `SKIPPED_REASON`-записей).
  Это не косметическая правка сигнатуры `storage_remedy`, а перестройка потока данных в
  `task_context.py`.
- `reviewer/config/fetch_errors.py` (весь файл, 41 строка) — образец, названный в задаче: судит по
  `response.status_code`/именам классов MRO, никогда не читает `str(exc)`/`args`/`url`. Показывает
  подход «классификация в закрытый набор меток», а не «редактирование сырого текста» — это два разных
  прочтения фразы «фильтр по образцу», см. Constraints.
- `reviewer/entrypoints/cli.py:840` (`def check`), детали `:878-887` и `:900-903` — **та же болезнь,
  независимая реализация**: `reviewer check` уже сам решает показывать подсказку `reviewer start`
  (`local_storage_down`, печать на `:905-908`) чисто по `_is_loopback_endpoint(s.pg_dsn)` /
  `_is_loopback_endpoint(s.neo4j_uri)`, эксепшн `e` в этом решении не участвует — хотя `e` уже пойман
  тут же, в `except`-блоке (в отличие от `task_context.py`, здесь эксепшн физически доступен в момент
  решения). Заодно на `:878-885` уже есть прецедент печати `str(e)` пользователю целиком
  (`click.echo(f"✗ Postgres: {err}")`) в ветке, где `err` не содержит «chunks»/«does not exist» —
  то есть кодовая база уже допускает показ сырого текста psycopg-исключения в CLI-контексте.
- `reviewer/tasks/boards/errors.py:19-28` — `sanitize_provider_text(value, secrets=())`: заменяет
  literal-значения (переданные явно) на `[REDACTED]` в тексте. Поскольку host/port/user уже известны
  из `storage_endpoints()`, эту готовую функцию можно переиспользовать для вымарывания вместо новых
  regex — прямая альтернатива самодельному фильтру.
- `reviewer/tasks/service.py:195,238,261,343` — 4 сайта потребления `is_storage_unavailable` (не
  `storage_remedy`); blast radius пуст, т.к. классификация `is_storage_unavailable` не меняется по
  условию задачи (критерий 4) — упомянуто для полноты обхода потребителей.
- (dropped 19 из ранжированной выдачи ретрива: session/pool/graph-store init, web/app.py, install.py,
  gitutil.py, chunker.py, vcs/gitlab.py, assemble.py, yougile.py, repo_id.py, vcs/base.py и т.п. —
  инфраструктурные сниппеты не по теме auth/missing-db, ретрив в задачу не попал вовсе (см. payload))

## Test exemplars
- `tests/test_storage_health.py:16-17,40-58` — юнит-тесты самого модуля: фикстуры `OperationalError`,
  параметризованные loopback/remote эндпоинты. Место для новых кейсов auth-fail/missing-db текста.
- `tests/config/test_fetch_errors.py:64-72` — `test_result_never_carries_exception_text`: эталонный
  паттерн для критерия 3 («ни пароль, ни DSN в диагностику») — секрет кладётся в `str(exc)` и в
  `.request.url`, проверяется `secret not in repr(result)`.
- `tests/config/test_fetch_errors.py:40-41` — `test_timeout_wins_over_connection_in_class_name`:
  прецедент «порядок паттернов важен» (класс содержит оба маркера) — тот же риск при одновременном
  совпадении «password authentication failed» и «does not exist» в одном сообщении.
- `tests/mcp/test_prepare_task_context.py:314-321` (`test_storage_failure_names_cause_and_remedy`) и
  `:353-365` (`test_remote_deploy_gets_cause_without_remedy`) — **регрессионные якоря**: оба жёстко
  утверждают `remedy == "reviewer start"` для generic `OperationalError("connection refused")` —
  ровно кейс «контейнеры действительно упали». Новая текстовая классификация обязана оставить эти
  тесты зелёными, подавляя remedy только для распознанных auth/missing-db паттернов.
- `tests/mcp/test_prepare_task_context.py:381-388` (`test_existing_gaps_keep_section_and_reason`) —
  `assert set(entry) == {"section", "reason", "cause", "remedy"}` — guard на форму `gap()`; если
  решение добавит новый ключ в словарь, этот тест придётся осознанно обновить.
- (dropped 13 из ранжированной выдачи test_exemplars payload — общие fail-soft/mock-паттерны
  (`test_graph_format`, `test_cli.py#test_check_fails_on_postgres_error` с `side_effect=RuntimeError`
  без текстовой классификации, `test_history.py` с битым DSN → `[]`, скилл-guard тесты и т.д.), ни
  один не про различение auth-сбоя/missing-db по тексту)

## Constraints / open questions
- **Архитектурный блокер, не косметика.** `task_context.py` вычисляет `remedy` ОДИН раз на старте
  `build_task_context` (`_StorageState(_remedy(deps))`, строка 138) — до первого реального сбоя.
  Эксепшн ловится позже в `_safe`, но туда не передаётся. Текстовая классификация требует переноса
  вычисления remedy внутрь `except`-ветки `_safe` (строки 76-85) с сохранением результата первой
  ошибки для последующих `SKIPPED_REASON`-записей той же короткой цепочки. Правка не сводится к
  добавлению параметра `exc` в `storage_remedy()` — меняется поток данных `_StorageState`/`_remedy`/
  `_storage_gap`.
- **Докстринг модуля противоречит замеру задачи.** `storage_health.py:8-13` заявляет «решение по
  типу, а не по тексту: в тексте живёт пароль» как причину дизайна; PRI-269 замерил, что пароль в
  `str(exc)` не попадает (хост/порт/юзер/имя БД — попадают). Разрешение противоречия (переписать
  докстринг? оставить решение по типу для `cause`, а текст завести только для нового поля?) — решение
  для брейнсторминга, не решённое здесь.
- **Два разных прочтения «фильтр по образцу `config/fetch_errors.py`».** (а) классификация в закрытый
  enum причины (аналог `classify_fetch_error` → `(transport, http_status)`, сырой текст никуда не
  просачивается) — безопаснее; (б) вымарывание host/port/user из сырого текста и показ остатка как
  `reason` — ближе к формулировке задачи («вымарать... оставив класс причины»), но требует regex/
  `sanitize_provider_text`-фильтра и более уязвимо к забытым полям. Задача явно требует «оставить
  класс причины», что ближе к (а), но пункт 2 говорит именно «вымарать» текст, что предполагает (б).
  Не решать заранее — фиксировать выбор на брейнсторминге.
- **`reviewer check` (`cli.py:840`) — тот же класс бага, вне явного скоупа задачи.** Independent
  implementation через `_is_loopback_endpoint` напрямую, не через `storage_remedy`. Стоит явно решить,
  входит ли починка этого места в PRI-277 или заводится отдельно — иначе получится два разных
  поведения (`reviewer check` vs MCP `gap.remedy`) для одного и того же диагноза.
- **Скоуп ошибок — Postgres, не Neo4j.** Замер задачи снят только на `psycopg.OperationalError`
  (пароль/несуществующая БД). Neo4j `AuthError` уже исключён из `is_storage_unavailable` целиком
  (не доходит до remedy-логики вовсе), а `ServiceUnavailable`/`SessionExpired` не имеют измеренного
  auth/missing-db аналога — расширять текстовую классификацию на neo4j без отдельного замера не стоит.
- **Риск коллизии паттернов.** «password authentication failed» и «database ... does not exist» в
  одном тексте пока не наблюдались одновременно, но libpq с multi-host DSN (несколько `hostaddr`)
  теоретически может склеить сообщения по нескольким хостам в одну строку — порядок проверки
  паттернов может иметь значение (прецедент — `fetch_errors.py:35-36`, комментарий про `ConnectTimeout`).
- **`reviewer` git ref для этой задачи (branch=main) отстаёт от факта.** `preflight.indexed_sha`
  (`6554f43a93a29e5f96ddc1834ac4e2fb6b9a728c`) — это ЛОКАЛЬНЫЙ `main` ref, который отстаёт от
  `origin/main` (`2a8dc49`) на 938 коммитов и не содержит `storage_health.py`/`task_context.py`
  вовсе — этим объясняется, почему штатный ретрив их не нашёл. `origin/main` уже содержит эти файлы
  побайтово идентичными локальному рабочему дереву (сверено `git diff origin/main` — пусто) — все
  path:line в этом брифе валидны против актуального `origin/main`/`dev`, но локальный индекс reviewer
  для тега `main` нуждается в `reviewer index --ref main` после `git fetch`, иначе следующее
  обращение к ретриву на этой ветке останется слепым к обоим файлам.

Собран на: mid (Sonnet), сборка: subagent
