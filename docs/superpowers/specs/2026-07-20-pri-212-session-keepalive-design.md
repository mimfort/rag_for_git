# Keepalive сессии ревью: живость по последней активности (PRI-212)

Задача: https://ru.yougile.com/team/686c049c8af8/#PRI-212
Бриф: `docs/superpowers/briefs/2026-07-20-PRI-212-session-keepalive.md`
Предшествующая спека: `docs/superpowers/specs/2026-07-14-overlay-gc-design.md` (PR #110)

## Проблема

GC осиротевших overlay (`reviewer/services/gc.py`) считает overlay `pr:N` живым, пока в
`review_sessions` есть строка с `created_at > now() - review_session_ttl_hours` (дефолт 24 ч).
`created_at` бампается только в `SessionStore.save()` — то есть при `prepare_review` — и не
продлевается активностью ревью. Ревью, работающее дольше TTL, формально делает собственный
overlay сиротой: параллельный `prepare_review` другого PR или `reviewer gc` снесёт `pr:N`
из-под идущего анализа. Тот же TTL используется для регидрации (`SessionStore.load`) и уборки
строк (`delete_expired`). Поднято ревьюером PR #110 как R2, осознанно отложено.

## Цели

1. Ревью, активно работающее дольше `review_session_ttl_hours`, не теряет свой overlay:
   параллельный `prepare_review` / `reviewer gc` его не трогают.
2. Брошенное ревью (никаких обращений) по-прежнему собирается GC через TTL — сироты не
   становятся бессмертными.
3. Инвариант GC сохранён: «не знаю живых» ≠ «живых нет» (сбой БД → не удалять ничего).
4. Единая семантика живости: «живой для GC» = «живой для регидрации» = «не удаляется
   `delete_expired`». TTL становится idle-таймаутом (от последней активности), а не сроком
   с момента prepare.

## Не-цели

- Настройка интервала троттлинга touch (константа, не Settings — YAGNI).
- Изменение `reviewer/services/gc.py` — не требуется и не производится: алгоритм GC,
  порядок чтений T1 (`list_overlay_refs`) → T2 (`live_keys`) и все его инварианты нетронуты.
- Продление жизни сессии без активности (никаких фоновых heartbeat-процессов).

## Решение (обзор)

Вариант (а) из задачи: отдельная колонка `last_seen_at` + лёгкий метод `SessionStore.touch()`.
Живость везде считается единым предикатом `COALESCE(last_seen_at, created_at)`. Точка
продления — `MCPReviewService._session()`: единственная воронка всех обращений к сессии
(тулы через `_invoke_tool`, `submit_findings`/`submit_verdicts`, `publish_review`,
кэш-хиты). In-memory зеркало — новое поле `_Session.last_seen_at`, по которому
`_gc_overlays` фильтрует `active_keys`.

## Схема данных (`reviewer/mcp/session_store.sql`)

- Колонка `last_seen_at timestamptz` — **nullable, без DEFAULT**.
- Миграция аддитивна и идемпотентна: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
  (по прецеденту миграций `review_findings`); новая таблица создаётся сразу с колонкой.
- Старые строки остаются с `NULL` — живость для них считается по `created_at`; никакого
  искусственного «освежения» на миграции.

## `SessionStore` (`reviewer/mcp/session_store.py`)

- `save()` — ставит `last_seen_at = now()` и в INSERT, и в ON CONFLICT-ветке: повторный
  `prepare_review` = новая сессия, оба поля сбрасываются честно.
- **Новый `touch(repo, pr)`** — `UPDATE review_sessions SET last_seen_at = now()
  WHERE repo = %s AND pr_number = %s`. Fail-soft (сбой — только `log.warning`).
  **Не создаёт строку**: если строки нет (персист упал ещё на `save`), touch — no-op;
  такую сессию страхует in-memory-путь (`active_keys`).
- Единый предикат живости `COALESCE(last_seen_at, created_at)` в трёх местах:
  - `load`: `... > now() - make_interval(hours => %s)`;
  - `live_keys`: то же условие;
  - `delete_expired`: `... <= now() - make_interval(hours => %s)`.
- Контракты ошибок не смешиваются: `touch`/`save`/`delete_expired` — fail-soft;
  `live_keys` — осознанно НЕ fail-soft (сбой БД пробрасывается, GC не должен спутать
  «прочитать не удалось» с «живых нет»).

## Сервисный слой (`reviewer/mcp/service.py`)

- `_Session`: новое поле `last_seen_at: datetime` (default_factory = now, как `started_at`)
  и отметка троттлинга `db_touched_at: datetime | None = None`.
- `_session(repo, pr)` — точка продления. На каждом обращении (кэш-хит и после регидрации):
  1. `s.last_seen_at = now()` — in-memory, всегда;
  2. DB-`touch()` — только если `db_touched_at` пуст или старше `_TOUCH_INTERVAL_S = 60`
     секунд (модульная константа). Тулы зовутся LLM-темпом; минутная гранулярность ничего
     не теряет при TTL в часах и убирает бессмысленно частые UPDATE.
- `_gc_overlays`: in-memory `active` фильтруется по `s.last_seen_at` (вместо `s.started_at`).
  `started_at` остаётся чистым «моментом создания» для `duration_ms` в истории (PRI-209) —
  семантическая коллизия из брифа снята разделением полей.
- Регидрация (`_rehydrate_session`) не меняется: свежая `_Session` получает
  `last_seen_at = now()` по default_factory, а первый же `_session()`-доступ выполнит
  DB-touch — сам факт регидрации продлевает строку в Postgres.
- `prepare_review` не меняется: `save()` уже проставляет `last_seen_at`.

## Обработка ошибок

- Сбой `touch` не роняет tool-call (fail-soft); in-memory `last_seen_at` при этом бампнут —
  страховка `active_keys` в текущем процессе работает и без БД.
- Брошенное ревью: обращений нет → `COALESCE(last_seen_at, created_at)` замирает → GC
  собирает overlay через TTL (цель 2).
- Сбой БД в GC-пути: `live_keys` бросает, `purge_orphaned_overlays` не удаляет ничего —
  инвариант «не знаю живых» ≠ «живых нет» не затронут (цель 3), gc.py не меняется.

## Тестирование (TDD, red-first)

- **Красный тест сценария PRI-212** (`tests/mcp/test_gc_on_prepare.py`): сессия с
  `started_at` старше TTL, но свежей активностью (недавний `last_seen_at`) → GC при
  `prepare_review` её overlay **не** трогает. Сейчас падает: фильтр идёт по `started_at`.
- Fake-store (`tests/mcp`): tool-call через `_invoke_tool` вызывает `touch`; два обращения
  подряд → один DB-touch (троттлинг); сбой touch (boom-store) не роняет tool-call.
- Regression-якорь: C4-тест `test_prepare_purges_overlay_of_stale_in_memory_session_past_ttl`
  остаётся зелёным — сессия без активности собирается как прежде.
- Integration (`tests/mcp/test_session_store.py`, реальный PG, `@pytest.mark.integration`):
  состарить `created_at` UPDATE-ом → без touch строка невидима для `live_keys`/`load` и
  удаляется `delete_expired`; после `touch` — жива во всех трёх (единый предикат);
  legacy-строка с `NULL last_seen_at` ведёт себя по `created_at`.
- Существующие тесты `tests/services/test_gc.py` не меняются (gc.py нетронут).

## Документация

- CLAUDE.md: обновить описание GC (живость = последняя активность, не создание).
- README.md + README.ru.md: упоминание keepalive в разделе про GC/сессии (память проекта:
  правки фич синхронно отражаются в обоих README).
