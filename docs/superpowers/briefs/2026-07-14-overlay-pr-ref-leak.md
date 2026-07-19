# Brief — Утечка overlay-ref pr:N (эфемерный индекс PR не удаляется)

## Task

Ожидание (CLAUDE.md): overlay `ref="pr:N"` в Postgres удаляется автоматически после
`publish_review`, и fail-soft при сбое `prepare`. Факт: `mimfort/rag_for_git`,
`ref="pr:94"` — 228 чанков в базе, PR #94 смержен 2026-07-03T01:00:15Z, но overlay
жив до сих пор (2026-07-14). Нужно: (1) установить настоящую причину утечки по коду,
(2) спроектировать защиту от НОВЫХ утечек (все пути, не только один), (3) убрать уже
осиротевший мусор (`pr:94` и, потенциально, другие). Приёмка: overlay не переживает ни
один сценарий незавершённого ревью; существующий мусор вычищен.

## Related work

- ID-99 [done] «Потеря сессии при рестарте reviewer-mcp между prepare и publish» —
  переиспользовать: это прямой предшественник в той же области (session lifecycle),
  но закрывал только *crash-recovery* (см. `reviewer/mcp/session_store.py`,
  `session_serde.py`) — НЕ случай, когда `publish_review` вообще никогда не
  вызывается (без рестарта). Тот фикс — часть проблемы, не решение текущей утечки.
- ID-95 [done] «Purge orphaned tasks on sync» — переиспользовать как архитектурный
  прецедент: `TaskService.purge_orphaned_tasks` (`reviewer/tasks/service.py:341`) —
  паттерн «explicit purge-тул с active-set извне» уже есть в кодовой базе для другой
  сущности (задачи); можно скопировать форму API (dict `{pruned, kept}`, fail-soft
  по слоям), но НЕ прямой источник active-set для overlay (там active-set — открытые
  PR, у GC оверлеев такого оракула из коробки нет).
- (dropped 6: ID-115/125/159/173/207/149 — релевантны субсистеме reviewer-mcp/index
  по эмбеддингу, но не про lifecycle overlay/сессии конкретно — просмотрены, не
  информируют root cause или фикс).

## Subsystems

- `reviewer/mcp` — `MCPReviewService`: сессии PR (prepare/regidration/publish/cleanup),
  `SessionStore` (Postgres-персист сессий, TTL только на чтении).
- `reviewer/services` — `ReviewService.prepare`: self-healing delete_ref в начале +
  except-очистка при сбое; single point, где overlay строится (`build_overlay`).
- `tests/mcp`, `tests/index`, `tests/services` — юнит-покрытие session lifecycle и
  overlay-гигиены (см. Test exemplars).

## Root cause (findings)

Все факты ниже — из реально прочитанного кода (не гипотезы), плюс подтверждение из БД.

1. **Overlay удаляется только в двух местах — ни одно из них не срабатывает, если
   `publish_review` для (repo, pr) НИКОГДА не вызывается:**
   - `reviewer/services/review_service.py:177` — self-healing `delete_ref(repo, f"pr:{pr_number}")`
     в НАЧАЛЕ `ReviewService.prepare()`, срабатывает только при СЛЕДУЮЩЕМ
     `prepare_review` для ТОГО ЖЕ (repo, pr).
   - `reviewer/services/review_service.py:348-357` — except-блок `prepare()`: чистит
     overlay при сбое ВНУТРИ prepare (до возврата `PreparedReview`).
   - `reviewer/mcp/service.py:1178-1194` (`MCPReviewService._cleanup`) — вызывается
     ТОЛЬКО из `publish_review` (`reviewer/mcp/service.py:1042`, безусловно, «ВСЕГДА»
     по докстрингу на `reviewer/mcp/service.py:920») — но это «всегда» означает
     «всегда, если `publish_review` вообще вызван», а не гарантию, что он будет
     вызван.
   - **Вывод**: если `prepare_review` успешно построил overlay (build_overlay в
     `reviewer/services/review_service.py:274-282`), но пайплайн ревью прервался
     ДО `publish_review` (юзер отменил, оркестрирующая Claude Code-сессия
     упала/таймаутнула, verify/analyze subagent зациклился и не дошёл до шага 6) —
     overlay остаётся в Postgres НАВСЕГДА, если тот же PR больше никогда не
     ре-ревьюится (типичный случай: PR смержен → его больше не открывают на ревью).
   - Других мест удаления `pr:*` ref в кодовой базе нет — `grep -rn "delete_ref"
     reviewer/` даёт только эти 3 сайта (2 в review_service.py, 1 в mcp/service.py)
     плюс сам метод `ChunkStore.delete_ref` (`reviewer/index/store.py:147-151`).

2. **Нет TTL/GC для `chunks`-таблицы вообще.** `reviewer/index/schema.sql:4-17` —
   таблица `chunks` не имеет столбца `created_at`/`updated_at` (есть только у
   `index_meta`, `subsystem_summaries`, `repo_vcs`). Физически невозможно построить
   time-based purge по overlay-чанкам без миграции схемы — сейчас не на чем его
   основать.

3. **Нет CLI/MCP-команды для ручной/периодической уборки overlay.** `grep -ni
   "purge\|overlay\|gc\b\|cleanup\|reap\|stale" reviewer/entrypoints/cli.py` — из
   overlay-релевантного только `services/status.py` (см. п.4, read-only). Ни
   `reviewer/entrypoints/cli.py`, ни `reviewer/entrypoints/mcp_server.py` не
   регистрируют команду/тул уборки overlay. Для сравнения: у задач такой тул ЕСТЬ
   (`purge_orphaned_tasks`, `reviewer/mcp/service.py:332-338`) — для overlay
   аналога нет.

4. **`reviewer status` только показывает overlay, не чистит.**
   `reviewer/services/status.py:67-71` (`build_status_report`) собирает
   `OverlayStatus(ref, chunks)` через `store.list_refs(repo)` (`reviewer/index/store.py:250-256`)
   и `store.count_chunks` — чисто read-only отчёт (см. вывод CLI: `Overlay: pr:94
   228 чанков`), без возраста (нет `created_at`, см. п.2) и без действия.

5. **Нет boot-time reaper в MCP-сервере.** `grep -n "lifespan|startup|atexit"
   reviewer/entrypoints/mcp_server.py` — ничего не найдено; при старте процесса
   `reviewer-mcp` никакая просроченная сессия/overlay не подчищается.

6. **Параллельная (родственная) утечка: строки `review_sessions` не удаляются по
   TTL — TTL применяется ТОЛЬКО как условие `WHERE` при чтении.**
   `reviewer/mcp/session_store.py:87-103` (`SessionStore.load`) фильтрует
   `WHERE created_at > now() - make_interval(hours => %s)` — просроченная строка
   становится «невидимой» для `_rehydrate_session`, но САМА СТРОКА остаётся в
   таблице `review_sessions` навсегда (никто не вызывает `SessionStore.delete`
   по TTL, только `_cleanup` после реального `publish_review`,
   `reviewer/mcp/service.py:1195-1197`). Подтверждено данными: строка `(repo=
   mimfort/rag_for_git, pr_number=94, created_at=2026-07-03 20:25:31+00)` до сих
   пор в `review_sessions` спустя 11 дней (TTL по умолчанию 24ч,
   `reviewer/config/settings.py:56`) — просрочена, но жива.

7. **Подтверждение по данным (не гипотеза — установленный факт):**
   PR #94 смержен `2026-07-03T01:00:15Z` (`gh pr view 94`). Строка сессии
   `review_sessions` для pr_number=94 создана `2026-07-03 20:25:31+00` — то есть
   `prepare_review("mimfort/rag_for_git", 94)` был вызван почти через 20 часов
   ПОСЛЕ мержа (вероятно, повторное/тестовое ревью уже закрытого PR). При этом
   `review_runs` не содержит НИ ОДНОЙ строки с `pr_number=94` — `publish_review`
   для этого PR не выполнился ни разу (ни успешно, ни с ошибкой: путь ошибки в
   `_record_history`, `reviewer/mcp/service.py:1109-1176`, тоже пишет строку в
   `review_runs`, даже при `status="error"` — раз строки нет вообще, значит
   `publish_review` не был вызван, а не просто упал). Это прямое подтверждение
   гипотезы «prepare без publish»: `_cleanup` (единственный код-путь, который бы
   удалил `pr:94`) никогда не выполнился.

8. **Косвенный факт про плагин-оркестратор** (не код reviewer, а LLM-скилл):
   `plugin/skills/review-pr/SKILL.md` раздел «Failure handling» (строки 128-136)
   описывает три сценария отказа (упавший analyze-субагент, `status: "skipped"`,
   сбой самого `prepare_review`) — но НЕ описывает и не гарантирует вызов
   `publish_review` при отмене пользователем / краше/таймауте оркестрирующей
   сессии Claude Code между шагом 1 и шагом 6. Скилл — это промпт для LLM, не код
   с `try/finally`: гарантий на уровне рантайма нет в принципе для этого случая —
   утечка архитектурно неизбежна без server-side GC, независимо от того, как
   аккуратно написан SKILL.md.

## Relevant code

- `reviewer/mcp/service.py:1178-1194` (`MCPReviewService._cleanup`) — единственная
  точка удаления overlay на «счастливом» пути; кандидат на добавление вызова из
  нового GC-механизма/тула.
- `reviewer/services/review_service.py:156-369` (`ReviewService.prepare`), особенно
  `:177` (self-heal) и `:348-357` (except-cleanup) — существующие частичные защиты,
  контекст для решения, не трогать логику без необходимости.
- `reviewer/index/store.py:147-151` (`ChunkStore.delete_ref`) — уже есть, blast radius:
  вызывается из `review_service.py:177,352` и `mcp/service.py:1192` (3 сайта,
  подтверждено `callers`).
- `reviewer/index/store.py:250-256` (`ChunkStore.list_refs`) — уже перечисляет все
  ref репо; естественная точка для нового «list orphaned overlays» запроса
  (нужен доп. критерий возраста/активности — сейчас `chunks` без timestamp, см.
  Root cause п.2).
- `reviewer/index/schema.sql:4-17` (таблица `chunks`) — если решение будет
  time-based (TTL по overlay), потребуется миграция: добавить `created_at`/
  `updated_at` в `chunks` (forward-only `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`,
  по образцу `chunks.repo` на строке 19).
- `reviewer/mcp/session_store.py:105-115` (`SessionStore.delete`) — уже умеет
  удалять по (repo, pr); нужен способ найти ПРОСРОЧЕННЫЕ строки массово (сейчас
  нет `list_expired`/`delete_expired`, только `load` с WHERE-фильтром и точечный
  `delete`).
- `reviewer/mcp/session_store.py:20` (`_SCHEMA` = `session_store.sql`) — там же
  `review_sessions_created_idx` (индекс по `created_at`) уже есть — GC-запрос по
  возрасту сессий был бы дешёвым.
- `reviewer/services/status.py:49-72` (`build_status_report`) /
  `reviewer/entrypoints/cli.py` (команда `status`, строки ~510-530) — если решение
  включает видимость возраста overlay в `reviewer status`, здесь нужно расширять
  `OverlayStatus`.
- `reviewer/tasks/service.py:341-380` (`purge_orphaned_tasks`) — форма-прецедент
  для нового purge-тула (если выбран путь «explicit GC MCP-тул»): dict-отчёт,
  fail-soft по слоям, аргумент управления поведением (`keep_with_prs` ~ аналог
  «не трогать активные PR»).
- `reviewer/entrypoints/mcp_server.py` — нет lifespan/startup hook; если решение —
  boot-time reaper, добавлять здесь (сейчас файл содержит только регистрацию
  тулов, `main()` на строке 349).
- (dropped: `reviewer/tools/code_tools.py`/`ToolContext` — участвует в сессии, но
  не в её удалении/создании; не релевантно для этой утечки).

## Test exemplars

- `tests/mcp/test_publish.py:238-246` (`test_publish_cleans_overlay_even_on_vcs_error`) —
  паттерн: мокает `build_overlay`/`chunk_python`, проверяет
  `store.deleted_refs.count("pr:7") == 2` (self-heal + cleanup) даже при сбое
  публикации в VCS. Хороший образец для теста «сколько раз и когда delete_ref
  реально вызывается» — FakeStore трекает `deleted_refs` как список.
- `tests/services/test_review_service.py:214-231` (`test_prepare_closes_internal_vcs_on_failure`) —
  паттерн проверки порядка/числа вызовов `delete_ref` через
  `components.store.delete_ref.call_args_list` (MagicMock).
- `tests/mcp/test_session_persist.py:120-131` (`test_publish_after_restart_rehydrates_session`) —
  паттерн эмуляции рестарта процесса (`svc._sessions.clear()`), полезен как основа
  для нового теста «prepare, затем НИКОГДА publish → overlay остаётся» (сейчас
  такого теста НЕТ — см. ниже).
- `tests/mcp/test_session_store.py:28-50` (`test_session_store_save_load_delete_ttl`,
  `@pytest.mark.integration`) — паттерн проверки TTL на `SessionStore.load`
  (`store.load(repo, pr, 0) is None` при TTL=0). Показывает, что тестируется
  только «TTL прячет строку от load», но НЕТ теста, что строка реально удаляется
  из таблицы по TTL — потому что такого поведения в коде нет (см. Root cause п.6).
- `tests/index/test_store_hybrid.py:34-49` (`test_delete_ref_removes_only_target_ref`,
  `@pytest.mark.integration`) — базовый юнит-паттерн для `ChunkStore.delete_ref`
  на реальном Postgres; переиспользовать для теста нового GC-запроса
  (list_refs/count_chunks по нескольким ref).
- **Пробел (нет прямого прецедента):** ни один существующий тест не покрывает
  сценарий «`prepare_review` вызван, `publish_review` НИКОГДА не вызывается,
  сервис живёт долго/перезапускается — overlay остаётся навсегда». Это ядро
  бага и одновременно отсутствующий TDD-якорь — фикс потребует написать такой
  тест первым (red), затем механизм GC (green). Аналогично нет теста на массовое
  удаление просроченных строк `review_sessions`.

## Constraints / open questions

- **Развилка дизайна (решить человеку): каким механизмом гасить утечку** —
  варианты не исключают друг друга:
  1. TTL-based периодическая уборка (нужна миграция схемы: `created_at` в
     `chunks`, см. Root cause п.2 / Relevant code про `schema.sql`);
  2. explicit CLI/MCP-тул уборки по образцу `purge_orphaned_tasks` (нужен
     источник «активных PR» — вызов GitHub API за списком открытых PR, что
     стоит квоты и требует токена; или более грубый критерий — «просто TTL по
     `list_refs`/сессиям без сверки с GitHub»);
  3. boot-time reaper в `reviewer-mcp` (чистит просроченные `review_sessions` +
     соответствующие overlay при старте процесса) — не помогает, если процесс
     не перезапускается месяцами (текущий кейс: если сервер жил без рестарта, ни
     TTL-чтение, ни boot-hook не сработали бы);
  4. `reviewer status`/`reviewer index` (уже периодически гоняется человеком)
     дополнительно чистит orphaned overlay как побочный эффект — самый дешёвый
     по инфраструктуре, но неявный/сюрпризный побочный эффект read-статус-команды.
  - Скорее всего нужна комбинация (минимум TTL-миграция + explicit purge-тул),
    но выбор — не работа этого брифа.
- **`chunks` не имеет timestamp вообще** — любое time-based решение начинается с
  ADD COLUMN миграции (forward-only, `IF NOT EXISTS`, по образцу существующих
  миграций в `schema.sql`), это не «просто добавить проверку», а изменение схемы
  на проде.
- **Деплой reviewer-mcp может быть старше кода этого фикса.** По CLAUDE.md
  (см. паттерн PRI-205/PRI-207 в MEMORY) — клиентские скиллы (`plugin/`) ходят в
  уже задеплоенный сервер; если фикс требует нового MCP-тула
  (например, `purge_orphaned_overlays`) — он появится в **деплое** не сразу
  после мержа кода, а после отдельного передеплоя. Любой клиентский скилл,
  который бы полагался на новый тул, должен деградировать fail-soft, если тул
  недоступен (как это сделано для `sync_board`/`finish_task`/`get_board_targets`).
- **`pr:94` конкретно** — можно вычистить вручную одной командой
  (`DELETE FROM chunks WHERE repo='mimfort/rag_for_git' AND ref='pr:94'` /
  `DELETE FROM review_sessions WHERE repo='mimfort/rag_for_git' AND pr_number=94`)
  независимо от выбора механизма — это не решает архитектурную проблему, только
  снимает текущий конкретный мусор; вычистка существующего мусора по ВСЕМ репо
  требует либо ручного прохода по `list_refs`, либо готового GC-тула (пункт
  «критерии приёмки» задачи явно требует и то, и другое).
- Задача **board-less** — нет ключа доски, привязать к задаче на доске не к чему;
  бриф не пытается резолвить `task_key`.

Собран на: средний тир (Sonnet-класс), режим: subagent

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 43 · out 18.1K · cache-write 119.7K · cache-read 1.3M
Всего: 1.4M токенов
