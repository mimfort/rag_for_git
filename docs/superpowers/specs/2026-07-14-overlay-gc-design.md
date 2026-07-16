# Дизайн — GC осиротевших overlay `pr:N` и просроченных сессий ревью

Бриф: `docs/superpowers/briefs/2026-07-14-overlay-pr-ref-leak.md`

## Проблема

Overlay `ref="pr:N"` (эфемерный индекс изменённых файлов PR) удаляется только на «счастливом»
пути — из `MCPReviewService._cleanup` (`reviewer/mcp/service.py:1178`), который вызывается
исключительно из `publish_review` (`reviewer/mcp/service.py:1042`). Если пайплайн ревью оборвался
между `prepare_review` и `publish_review` (пользователь отменил, оркестрирующая Claude Code-сессия
упала или упёрлась в таймаут), overlay остаётся в Postgres навсегда.

Два частичных предохранителя не спасают:

- self-heal в начале `ReviewService.prepare` (`reviewer/services/review_service.py:177`) чистит
  overlay только при **следующем** `prepare_review` для **того же** PR — а смерженный PR обычно
  больше никогда не ревьюится;
- except-блок `prepare` (`reviewer/services/review_service.py:348`) чистит overlay только при сбое
  **внутри** самого `prepare`.

Подтверждение по данным (не гипотеза): PR #94 смержен `2026-07-03T01:00:15Z`; строка сессии
`review_sessions` для `pr_number=94` создана `2026-07-03 20:25:31+00`; в `review_runs` нет ни одной
строки с `pr_number=94`. Путь ошибки в `publish_review` тоже пишет строку в `review_runs`
(со `status="error"`, `reviewer/mcp/service.py:1109-1176`) — значит `publish_review` не «упал», его
никогда не вызывали. В базе до сих пор висят 228 чанков `ref="pr:94"`.

**Родственная утечка.** Строки `review_sessions` не удаляются по TTL: TTL применяется только как
условие `WHERE` при чтении (`reviewer/mcp/session_store.py:87-103`). Просроченная строка становится
невидимой для `load`, но живёт в таблице вечно — строка для PR #94 всё ещё там спустя 11 дней при
TTL в 24 часа.

**Плагин чинить нечего.** `plugin/skills/review-pr/SKILL.md` — промпт для LLM, а не код с
`try/finally`. При отмене пользователем или краше оркестрирующей сессии никакой текст скилла не
выполнится. Гарантию может дать только server-side GC на стороне Python.

## Решение

Одна GC-функция, два вызывающих. Миграция схемы `chunks` не нужна: реестр «живых» overlay уже есть —
таблица `review_sessions` с `created_at` и ключом `(repo, pr_number)`, а строка сессии создаётся
ровно там же, где строится overlay.

**Критерий сироты:** overlay `pr:N` репозитория `R` — сирота, если для `(R, N)` нет непросроченной
строки в `review_sessions` и `(R, N)` нет среди in-memory сессий текущего процесса. TTL берётся из
существующей настройки `review_session_ttl_hours` (`reviewer/config/settings.py:56`, дефолт 24ч) —
той же, по которой `MCPReviewService` регидрирует сессию (`reviewer/mcp/service.py:201`), так что
«живой для регидрации» и «живой для GC» — одно и то же условие.

In-memory сессии учитываются потому, что `SessionStore.save` — fail-soft
(`reviewer/mcp/session_store.py:84`): при сбое persist сессия живёт только в памяти процесса, и без
этой страховки GC снёс бы overlay идущего ревью.

### Компоненты

**`reviewer/services/gc.py`** (новый) — `purge_orphaned_overlays(store, session_store, ttl_hours,
active_keys) -> dict`:

1. `live = session_store.live_keys(ttl_hours) | active_keys`
2. для каждой пары `(repo, ref)` из `store.list_overlay_refs()`: распарсить `N` из `pr:N`; если
   `(repo, N) not in live` → `store.delete_ref(repo, ref)`
3. `session_store.delete_expired(ttl_hours)`
4. вернуть `{"purged": [...], "kept": int, "sessions_deleted": int}` — форма отчёта по образцу
   `TaskService.purge_orphaned_tasks` (`reviewer/tasks/service.py:341`)

**`ChunkStore.list_overlay_refs() -> list[tuple[str, str]]`** (новый, `reviewer/index/store.py`) —
все пары `(repo, ref)` с `ref LIKE 'pr:%'` по всем репозиториям. Существующий `list_refs(repo)`
(`reviewer/index/store.py:250`) скоупится одним репо, а GC должен видеть базу целиком.

**`SessionStore.live_keys(ttl_hours) -> set[tuple[str, int]]`** и
**`SessionStore.delete_expired(ttl_hours) -> int`** (новые, `reviewer/mcp/session_store.py`) — сейчас
есть только точечный `delete(repo, pr)`. Индекс `review_sessions_created_idx` по `created_at` уже
существует (`reviewer/mcp/session_store.sql`), запрос по возрасту дешёвый.

### Вызывающие

**`MCPReviewService.prepare_review`** — вызывает GC в начале, передавая `active_keys =
set(self._sessions)`. Полностью fail-soft: любое исключение уходит в `log.warning` и не роняет
подготовку ревью. Это и есть гарантия «больше никогда»: брошенный overlay живёт максимум до
следующего ревью в этом деплое.

**CLI `reviewer gc`** (новая команда, `reviewer/entrypoints/cli.py`) — явная уборка с
человекочитаемым отчётом (сколько overlay удалено, сколько оставлено как живые, сколько просроченных
сессий вычищено). Ею же вычищается текущий осиротевший `pr:94`.

Существующие `self-heal` в `prepare` и `_cleanup` в `publish_review` остаются без изменений — они
закрывают счастливый путь, GC добирает остальное.

### Границы безопасности

- GC трогает **только** `ref LIKE 'pr:%'`; `base:<branch>` не затрагивается никогда.
- Параллельное ревью другого PR защищено: у него свежая строка сессии → он «живой».
- Ревью старше TTL нежизнеспособно и так: `SessionStore.load` его не отдаст, `publish_review` по нему
  не пройдёт — удалять его overlay безопасно.

## Тесты (TDD, red first)

Первым пишется тест на ядро бага — сейчас такого нет ни одного:

1. `prepare_review` вызван, `publish_review` — никогда; следующий `prepare_review` (для **другого**
   PR) подчищает осиротевший overlay. Образец мокинга: `tests/mcp/test_publish.py:238`
   (`test_publish_cleans_overlay_even_on_vcs_error`, FakeStore трекает `deleted_refs`).
2. Overlay PR с живой строкой сессии не тронут (параллельное ревью).
3. Overlay PR с живой in-memory сессией не тронут, даже если строки в БД нет (сбой fail-soft persist).
4. `base:<branch>` refs не тронуты.
5. Просроченные строки `review_sessions` удаляются (сейчас поведения нет вовсе —
   `tests/mcp/test_session_store.py:28` проверяет лишь, что TTL прячет строку от `load`).
6. Сбой GC не роняет `prepare_review` (fail-soft).
7. CLI `reviewer gc` печатает отчёт.
8. Integration (реальный Postgres): `list_overlay_refs` возвращает overlay всех репо и не возвращает
   `base:*`. Образец: `tests/index/test_store_hybrid.py:34`.

## Документация

`CLAUDE.md` и оба README (EN/RU) сейчас утверждают: «Overlay удаляется автоматически
(`store.delete_ref("pr:N")`) — после `publish_review` эфемерный ref не остаётся в Postgres». Это
неточно и ровно это ввело в заблуждение. Формулировку заменить на честную: overlay удаляется после
`publish_review`, а брошенные (ревью прервано до публикации) собирает GC — оппортунистически при
следующем `prepare_review` и по команде `reviewer gc`.

## Вне скоупа

- Миграция `chunks` (добавление `created_at`): не требуется — возраст overlay выводится из
  `review_sessions`.
- Сверка с VCS (открыт ли PR): стоит API-квоты, требует токена и ломается на ревью уже закрытого PR
  (ровно кейс `pr:94`).
- Boot-time reaper в `reviewer-mcp`: не помогает, если процесс живёт месяцами без рестарта.
- Изменения в `plugin/` (кроме документации): скилл не может дать рантайм-гарантию.
