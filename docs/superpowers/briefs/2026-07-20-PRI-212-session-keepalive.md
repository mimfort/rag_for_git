# Brief — PRI-212 Keepalive сессии ревью: анализ дольше TTL делает свой overlay сиротой для GC
https://ru.yougile.com/team/686c049c8af8/#PRI-212

## Task

Данные задачи получены из стора reviewer (`get_task` после `sync_board`), не переспрашивались повторно.
GC осиротевших overlay (`reviewer/services/gc.py`, PR #110) считает overlay `pr:N` живым, пока в
`review_sessions` есть непросроченная строка (TTL `review_session_ttl_hours`, дефолт 24ч), но
`created_at` не продлевается активностью — только на `save()` (т.е. на `prepare_review`). Ревью,
идущее дольше TTL, теряет собственный overlay: параллельный `prepare_review` другого PR или
`reviewer gc` снесёт его из-под работающего анализа. Тот же TTL используется для регидрации сессии
(`service.py:201`). Поднято ревьюером PR #110 как R2, осознанно отложено.
Критерии приёмки: (1) активное дольше TTL ревью не теряет overlay; (2) брошенное ревью по-прежнему
собирается GC через TTL; (3) инвариант «не знаю живых» ≠ «живых нет» сохранён.

## Related work

- ID-99 [done] «Потеря сессии при рестарте reviewer-mcp между prepare и publish» — переиспользовать
  как прямого предшественника в той же области (session lifecycle, `SessionStore`/`session_serde.py`,
  crash-recovery через персист); закрывал другой сценарий (рестарт процесса), не TTL-живость при
  непрерывной работе, но даёт форму решения (Postgres-персист сессии, регидрация).
- (dropped 6: ID-149 «кросс-субагентный кэш тулов», ID-125 «reviewer status», ID-141 «solve-task
  preflight: свежесть индекса», ID-117 «review-range A..B», ID-136 «интерактивный триаж находок»,
  ID-127 «петля обратной связи resolved/dismissed» — попали в top-8 по эмбеддингу subsystem'а
  reviewer-mcp/CLI, но не про lifecycle сессий/overlay/TTL; показано 8 из 30, остаток не запрашивался
  за отсутствием сигнала релевантности). `get_task_context` линкованных задач/PR не вернул (в графе
  задач PRI-212 не связан формально с PR #110 — тот упомянут только текстом в описании).

## Subsystems

- `reviewer/mcp` — сервисный слой MCP: `MCPReviewService` управляет сессиями PR (prepare/регидрация/
  вызов тулов/publish), `SessionStore` — Postgres-персист сессий.
- `reviewer/services` — `gc.py` (`purge_orphaned_overlays`) живёт здесь, оркестрация ревью
  (`ReviewService.prepare`) — соседний self-heal путь очистки overlay.
- `tests/mcp` — юнит-тесты MCP-слоя: сессии, регидрация, cleanup, история.
- `tests/services` — юнит-тесты `purge_orphaned_overlays` (весь текущий набор сценариев GC).
- (dropped 4: `reviewer/policy`, `tests/policy`, `reviewer/web`, `reviewer/tasks` — соседние по
  эмбеддингу reviewer-mcp/CLI кластеры, не касаются lifecycle сессий/overlay).

## Relevant code

- `reviewer/mcp/session_store.py:117-132` (`SessionStore.live_keys`) — определяет живость по
  `created_at` (`WHERE created_at > now() - make_interval(...)`), не по активности; **осознанно НЕ
  fail-soft** — сбой БД должен пробрасываться, чтобы GC не спутал «прочитать не удалось» с «живых
  нет» (докстрока прямо это объясняет). Любой новый keepalive-метод, встающий в этот же контракт
  чтения, должен сохранить это свойство.
- `reviewer/mcp/session_store.py:72-85` (`SessionStore.save`) — единственное место, где сейчас
  бампается `created_at` (`ON CONFLICT ... DO UPDATE SET ... created_at = now()`), и только на
  `prepare_review`. Fail-soft (сбой — только `log.warning`).
- `reviewer/services/gc.py:35-111` (`purge_orphaned_overlays`) — читает `live = session_store.live_keys(ttl_hours) | set(active_keys)`
  (:90) и удаляет overlay вне `live`. Комментарий :62-79 фиксирует критичный порядок чтений
  T1 (`list_overlay_refs`) → T2 (`live_keys`) — **не менять**.
- `reviewer/mcp/service.py:1248-1278` (`MCPReviewService._gc_overlays`) — считает in-memory
  `active = {k for k, s in self._sessions.items() if s.started_at > cutoff}` тем же TTL и передаёт как
  `active_keys`; вызывается из `prepare_review`.
- `reviewer/mcp/service.py:234-258` (`MCPReviewService._invoke_tool`) — **touch-точка**: единственное
  место, где вызываются тулы PR-сессии (`search_code`/`get_related_symbols`/`read_file`/
  `get_definition`/`find_callers`/`get_changed_file_diff`/`get_impact` через `make_tools(s.ctx)`).
  Естественное место добавить продление живости на каждое обращение. Blast radius (graph): у
  `make_tools` единственный вызывающий в проде — именно `_invoke_tool` (остальные caller'ы — тесты
  `tests/tools/test_code_tools.py`).
- `reviewer/mcp/service.py:216-232` (`MCPReviewService._session`) — кэш-lookup/регидрация; кандидат,
  если продление живости должно происходить и при попадании в кэш, не только при tool-call.
- `reviewer/mcp/service.py:188-214` (`MCPReviewService._rehydrate_session`) — грузит через
  `store.load(repo, pr, ttl_hours)` (:201, тот же TTL, что у GC); при регидрации **сбрасывает**
  `started_at`/`steps` (:212-213) — но не пишет ничего обратно в Postgres (`store.load`, не `save`).
- `reviewer/mcp/service.py:53-74` (`_Session`) — `started_at: datetime` (:73), единственный
  in-memory маркер свежести, используемый `_gc_overlays`; сейчас это «момент СОЗДАНИЯ сессии»
  (для `duration_ms` в истории, PRI-209), а не «последняя активность» — семантическая коллизия при
  переиспользовании под keepalive.
- `reviewer/entrypoints/cli.py:535-578` (команда `gc`) — второй вызывающий `purge_orphaned_overlays`,
  без `active_keys` (у CLI нет in-memory сессий процесса reviewer-mcp).
- `reviewer/tools/code_tools.py:60-168` (`make_tools`) — определяет 7 тулов PR-сессии; пересоздаётся
  на каждый `_invoke_tool`, но кэш (`ctx.cache`) и seen-дедуп живут в `ctx`/сессии.
- (при переизбытке контекста `search_codebase` дважды обрывался по cliff — 7 из 15 и 7 из 22
  результатов; остаток не запрашивался повторно, т.к. верхние результаты уже покрыли touch-точку и
  ядро GC).

## Test exemplars

- `tests/services/test_gc.py:52-60` (`test_keeps_overlay_with_live_session_row`) — базовый сценарий:
  непросроченная строка сессии → overlay сохранён; `_FakeSessionStore.live_keys` мокает БД множеством
  ключей.
- `tests/services/test_gc.py:63` (`test_keeps_overlay_of_active_in_memory_session`) — in-memory
  `active_keys` защищает overlay даже без строки в БД (fail-soft persist miss) — паттерн для будущего
  теста «touch продлевает БД-строку, а не только in-memory».
- `tests/services/test_gc.py:75` (`test_never_deletes_anything_when_live_set_unavailable`) —
  `_FakeSessionStore(boom=True)` → `live_keys` бросает → ничего не удаляется (инвариант «не знаю» ≠
  «нет»).
- `tests/services/test_gc.py:87` (`test_no_session_store_is_noop`) — `session_store=None` → полный
  no-op.
- `tests/services/test_gc.py:97` / `:113` (`test_ignores_unparsable_ref`, `test_ignores_base_ref_even_if_listed`) —
  граничные случаи парсинга `pr:N` и неприкосновенность `base:<branch>`.
- `tests/services/test_gc.py:126-152` (`test_reads_overlay_snapshot_before_live_keys`) — TOCTOU-guard
  на порядок T1/T2 через журнал вызовов (`calls: list[str]`), красный тест при откате порядка.
- `tests/mcp/test_gc_on_prepare.py:111-131` (`test_prepare_purges_overlay_of_stale_in_memory_session_past_ttl`, C4) —
  ОБРАТНЫЙ сценарий к тому, что должен чинить PRI-212: in-memory-сессия без активности, `started_at`
  старше TTL → сирота собирается. Хороший regression-якорь: новый keepalive-тест должен показать, что
  та же сессия с обращениями к тулам НЕ собирается.
- `tests/mcp/test_session_store.py:28-50` / `:69-105` (`test_session_store_save_load_delete_ttl`,
  `test_live_keys_and_delete_expired`, `@pytest.mark.integration`, реальный Postgres) — паттерн
  проверки TTL-семантики `save`/`load`/`live_keys`/`delete_expired`; докстрока :73-79 явно
  предостерегает от `delete_expired(0)` на общей БД (класс дефекта, аналогичный TRUNCATE без repo).

## Constraints / open questions

- Порядок чтений T1 (`list_overlay_refs`) → T2 (`live_keys`) в `purge_orphaned_overlays` — **не
  менять** (комментарий `gc.py:62-79`, защищено тестом `test_reads_overlay_snapshot_before_live_keys`).
- `SessionStore.save` — fail-soft; любой новый touch/keepalive-write должен наследовать это свойство
  (не ронять tool call при сбое БД). `SessionStore.live_keys` — наоборот, осознанно НЕ fail-soft;
  смешивать эти два контракта в одном методе нельзя.
- Вариант (б) из задачи (бампать `created_at` на каждом обращении, без миграции) сама задача
  помечает как худший — поле перестаёт значить «время создания». Вариант (а) — отдельное поле
  `last_seen_at` — предпочтителен, но требует миграции схемы `review_sessions` (аддитивно,
  `ADD COLUMN IF NOT EXISTS`, по прецеденту миграций `review_findings`/`subsystem_summaries` из
  CLAUDE.md) и решения, продлевает ли touch **и** окно регидрации (`service.py:201` использует тот же
  `ttl_hours`, что и GC — задача явно называет это тем же местом).
- `_rehydrate_session` (:212-213) сбрасывает `started_at`/`steps` в памяти, но ничего не пишет
  обратно в Postgres (`store.load`, не `save`) — если keepalive опирается на БД-поле, одной
  регидратации/чтения недостаточно для продления БД-живости, нужен явный touch.
- Единственная точка обращения к тулам сессии — `_invoke_tool` (`service.py:234-258`); естественное
  место для touch-вызова, но нужно решить, продлевать ли также при простом попадании в кэш
  `_session` (:216-232) без реального tool-call.
- Инвариант GC сохранён кодом уже сейчас: `session_store is None` → no-op; сбой `list_overlay_refs`
  → overlay не трогаются, исключение пробрасывается, но `delete_expired` всё равно выполняется
  (сессионная гигиена независима от overlay-листинга).
- Спека `docs/superpowers/specs/2026-07-14-overlay-gc-design.md` фиксирует: критерий живости — «нет
  непросроченной строки в `review_sessions` И нет среди in-memory сессий процесса»; TTL намеренно тот
  же, что у регидрации, чтобы «живой для регидрации» = «живой для GC» — любое решение PRI-212 не
  должно развести эти два условия молча.
- Пробел: нет существующего теста на прямой сценарий PRI-212 (активность продолжается дольше TTL →
  overlay выживает) — только обратный (C4, отсутствие активности → сирота). Это первый red-тест,
  который потребуется.
- Пробел: `get_task_context` не дал связанных PR/задач (PR #110 не залинкован формально в графе
  задач) — контекст о происхождении задачи восстановлен через `get_pr_diff(110)` (в основном
  документация: CLAUDE.md/README) и Read спеки, а не через граф задач.

Собран на: mid (Sonnet), режим: subagent
