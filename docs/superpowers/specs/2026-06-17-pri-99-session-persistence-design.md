# PRI-99 — Персистентность сессии reviewer-mcp между prepare и publish

- **Задача:** PRI-99 / ID-99 — «Потеря сессии при рестарте reviewer-mcp между prepare и publish»
- **Дата:** 2026-06-17
- **Статус:** дизайн утверждён, готов к плану реализации

## Проблема

`MCPReviewService._sessions: dict[(repo, pr), _Session]` (`reviewer/mcp/service.py`) живёт
только в памяти процесса. `_Session` хранит `PreparedReview` + `ToolContext`. Если процесс
`reviewer-mcp` упал, был перезапущен или Claude Code переподключился между `prepare_review` и
`publish_review` одного PR — сессия теряется. `_session()` бросает
`ValueError("Сессия … не найдена")`, и PR остаётся без ревью.

### Реальный объём (важно)

Текущий деплой запускает сервер в **stdio long-lived**-режиме: плагин поднимает
`uvx --from rag-reviewer reviewer-mcp` один раз на сессию Claude Code (`server.run()` — stdio),
процесс живёт между всеми тул-вызовами. Поэтому сессия теряется **не на каждый вызов**, а только
при:

- краше процесса `reviewer-mcp` (необработанное исключение, OOM);
- рестарте/переподключении Claude Code между стадиями;
- ручном перезапуске сервера при разработке.

Упомянутый в задаче «uvx-per-subprocess на каждый MCP-вызов» для текущего stdio **неактуален**.
Объём дизайна — **crash-recovery в рамках stdio**. Future-proof под headless/HTTP не закладываем
(см. «Вне объёма»).

## Решение (кратко)

`_sessions` остаётся горячим in-memory кэшем; добавляем персистентную «подложку» в Postgres.
`prepare_review` сериализует `PreparedReview` в таблицу `review_sessions`; `_session()` при промахе
кэша лениво поднимает строку обратно, пересоздавая несериализуемые части (`vcs`, `ctx`).

Ключевые наблюдения, делающие это дешёвым:

- `PreparedReview` почти полностью сериализуем. `prq: PullRequest`, `units: list[ReviewUnit]`,
  `policy: ReviewPolicy` — плоские dataclass'ы с JSON-дружелюбными полями (`asdict` ↔ `Cls(**d)`).
  Остальное — `str`/`list`/`dict`/`None`.
- Единственное несериализуемое поле — `vcs: VCSProvider` (живой httpx-клиент). Оно восстановимо
  через `ReviewService._create_vcs_provider(owner, name)`.
- `ctx: ToolContext` НЕ персистится — он целиком пересобирается из `prepared` существующим
  `MCPReviewService._tool_context(prepared)`.
- Overlay `pr:N` (Voyage-эмбеддинги изменённых файлов) уже персистится в Postgres внутри
  `build_overlay` и переживает рестарт. Восстановленная сессия читает тот же overlay → grounding и
  graph-expansion работают идентично исходной сессии.

## Архитектура и компоненты

### 1. `reviewer/mcp/session_store.py` — новый класс `SessionStore`

По образцу `reviewer/web/history.py::ReviewHistory`:

- тот же `PG_DSN`, ленивый `psycopg_pool.ConnectionPool` (создаётся при первом обращении,
  thread-safe init), свой `schema.sql`, метод `init_schema()`;
- все операции **fail-soft** (ловят исключение, логируют `warning`, не пробрасывают);
- API:
  - `save(repo: str, pr: int, payload: dict) -> None` — upsert строки;
  - `load(repo: str, pr: int, ttl_hours: int) -> dict | None` — читает payload с проверкой TTL
    прямо в `WHERE created_at > now() - make_interval(hours => %s)`; истёкшая/отсутствующая →
    `None`;
  - `delete(repo: str, pr: int) -> None`;
  - `close() -> None` — закрыть пул.

### 2. `reviewer/mcp/session_store.sql` — схема

```sql
CREATE TABLE IF NOT EXISTS review_sessions (
  repo       text NOT NULL,
  pr_number  integer NOT NULL,
  payload    jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (repo, pr_number)
);
CREATE INDEX IF NOT EXISTS review_sessions_created_idx ON review_sessions (created_at);
```

`upsert` через `INSERT … ON CONFLICT (repo, pr_number) DO UPDATE SET payload = EXCLUDED.payload,
created_at = now()`.

### 3. Сериализатор `PreparedReview` ↔ dict

Функции в `session_store.py` (или рядом в `service.py`):

- `to_payload(prepared: PreparedReview) -> dict` — явно собирает dict: `asdict` для `prq`/`units`/
  `policy`, копирует `repo`, `branch`, `patches`, `sources`, `changed_paths`, `changed_node_ids`,
  `skipped_paths`, `overlay_ref`, `changed_status`, `task_board`, `task_keys`. **Исключает `vcs`.**
  НЕ использовать `dataclasses.asdict(prepared)` целиком — он споткнётся на `vcs`.
- `from_payload(d: dict, vcs: VCSProvider) -> PreparedReview` — `PullRequest(**d["prq"])`,
  `[ReviewUnit(**u) for u in d["units"]]`, `ReviewPolicy(**d["policy"])`, остальные поля as-is,
  `vcs=vcs`. Обёрнут в try/except: при `TypeError`/`KeyError` (несовместимая схема payload между
  версиями) — лог `warning`, вернуть/трактовать как промах.

### 4. Изменения `MCPReviewService` (`reviewer/mcp/service.py`)

- **Ленивое создание стораджа** — метод `_ensure_session_store() -> SessionStore | None` по образцу
  `ReviewService._ensure_history()`: возвращает `None`, если `not settings.review_session_persist`
  **или** `self._vcs_factory is not None` (см. edge case ниже); иначе создаёт `SessionStore` из
  `settings.pg_dsn` + размеры пула. Так `mcp_server.main()` не меняется — проводки в конструктор не
  требуется.
- `prepare_review`: после `self._sessions[...] = _Session(...)` — `store.save(repo, pr,
  to_payload(prepared))` (fail-soft).
- `_session()`: при промахе `_sessions` — `store.load(repo, pr, ttl)`; если payload есть →
  `vcs = _create_vcs_provider(owner, name)`, `prepared = from_payload(payload, vcs)`,
  `ctx = _tool_context(prepared)`, положить `_Session` в `_sessions` (прогрев) и вернуть; иначе
  (нет строки / истёк TTL / БД недоступна / битый payload) — `ValueError` с recovery hint.
- `_cleanup()`: `store.delete(repo, pr)` рядом с `store.delete_ref(repo, "pr:N")` (fail-soft).

### 5. Настройки (`reviewer/config/settings.py`)

- `review_session_persist: bool = True` — включение персиста;
- `review_session_ttl_hours: int = 24` — TTL строки.

## Поток данных

### A. `prepare_review` (запись подложки)
1. `prepare()` строит `PreparedReview` (overlay `pr:N` уже записан в Postgres).
2. `_sessions[(repo, pr)] = _Session(prepared, ctx)` (как сейчас).
3. `_ensure_session_store()?.save(repo, pr, to_payload(prepared))` — fail-soft.

### B. Любой тул / `publish_review` (чтение через `_session()`)
```
_session(repo, pr):
  s = _sessions.get((repo, pr))
  if s: return s                                  # горячий путь, без БД
  store = _ensure_session_store()
  if store:
    payload = store.load(repo, pr, ttl_hours)     # TTL проверяется в WHERE
    if payload:
      vcs = _create_vcs_provider(owner, name)
      prepared = from_payload(payload, vcs)       # None/исключение → промах
      if prepared:
        ctx = _tool_context(prepared)
        _sessions[(repo, pr)] = _Session(prepared, ctx)   # прогрев кэша
        return _sessions[(repo, pr)]
  raise ValueError("Сессия для {repo}#{pr} не найдена или истекла — вызови prepare_review заново")
```
Регидрация прозрачна и для `_invoke_tool`, и для `publish_review` — обе идут через `_session()`.
После первой регидрации сессия снова горячая.

### C. `_cleanup` (после publish, всегда)
- `_sessions.pop(...)` + закрытие внутреннего `vcs` (как сейчас);
- `store.delete(repo, pr)` (fail-soft);
- `store.delete_ref(repo, "pr:N")` (как сейчас).

### D. TTL / брошенные сессии
- Истечение — лениво в `load()` (через `WHERE created_at`). Просроченная строка = промах →
  ошибка+hint.
- Брошенный prepare (без publish): строка живёт до TTL; overlay `pr:N` самозалечивается на следующем
  `prepare` того же PR (`delete_ref` в начале `prepare`).

## Обработка ошибок и edge cases

Принцип: персист — страховка, он никогда не роняет основной путь. Все операции `SessionStore`
fail-soft (как `ReviewHistory`).

| Сценарий | Поведение |
|---|---|
| Postgres недоступен на `save` | `warning`; `prepare_review` отдаёт обычный payload. Горячая сессия в памяти есть. |
| Postgres недоступен на `load` (промах кэша) | Регидрация невозможна → `ValueError` + hint. Не маскируем под успех. |
| Промах кэша + нет строки / истёк TTL | `ValueError`: «Сессия … не найдена или истекла — вызови prepare_review заново». |
| `delete` на cleanup упал | `warning`; строка истечёт по TTL. |
| Битый/несовместимый payload (изменилась схема dataclass) | `from_payload` ловит `TypeError`/`KeyError` → `warning`, трактуем как промах → ошибка+hint (без трейсбека). |

**Recovery hint.** `_session()` кидает `ValueError`, FastMCP отдаёт его как ошибку тула. Текст
делаем самодостаточным и actionable («вызови `prepare_review` заново»). Скилл `review-pr` уже
вызывает `prepare_review` первым; отдельная клиентская авто-recovery логика в этом объёме не
требуется.

**Edge case — `vcs_factory` (test-only).** После рестарта фабрика недоступна (живёт в памяти).
Решение: **персист включается только когда `_vcs_factory is None`** (реальный GitHub-деплой). При
factory-провайдере (юнит-тесты/eval-снапшоты) `save`/`load` пропускаются — регидрация подняла бы
реальный `GitHubProvider`, что неверно для снапшота. Совпадает с существующей развилкой
`if self._vcs_factory is None` в `prepare_review`/`_cleanup`.

**Edge case — размер `sources`.** Полные тексты файлов в JSONB. `REVIEW_MAX_FILES` ограничивает
число файлов; типичный PR — десятки КБ. Gzip не закладываем (преждевременно).

**Edge case — конкуренция.** Класс не thread-safe by design (sync-тулы FastMCP последовательны).
Персист добавляет БД-раунд-трипы, но не меняет модель конкуренции. Глубокая защита `_sessions` —
профильная задача ID-100.

## Тестирование

**Юнит (`tests/mcp/`, на фейках, без реального GitHub/Postgres):**
- `to_payload`/`from_payload` — round-trip `PreparedReview` (минус `vcs`): все поля совпадают,
  dataclass'ы восстановлены.
- `_session()` регидрация (главный тест): prepare кладёт строку → очищаем `_sessions` (эмуляция
  рестарта) → тул/`publish_review` успешно регидрируют и работают. Используем фейковый
  `session_store` + инжектируемую VCS-фабрику в обход реального GitHub.
- Промах: пустой store → `_session()` кидает `ValueError` с recovery-hint текстом.
- Fail-soft: `save`/`load`/`delete` бросают → `prepare_review` всё равно возвращает payload; промах
  на `load` → ошибка+hint без трейсбека.

**Integration (маркер `integration`, поднятый Postgres):**
- реальный `SessionStore` на `PG_DSN`: схема создаётся, round-trip, TTL-выражение в `WHERE`
  работает (вставка с искусственно старым `created_at` → `load` возвращает `None`).

## Вне объёма (явно)

- Future-proof под headless CLI (ID-104) и HTTP/multi-worker деплой (где нет закреплённого процесса
  и каждый вызов может прийти в другой воркер). Дизайн совместим (персист в общей БД), но
  stateless-гарантии для всех тулов и согласование с ID-100 не закладываются здесь.
- Fallback-публикация findings as-is без grounding — отклонено: противоречит ядровому инварианту
  grounding (анти-галлюцинация file:line).
- Фоновый sweep просроченных строк (`sweep_expired`) — опциональное расширение; лениво-истекающего
  `load` + самозалечивания overlay достаточно для объёма.
- Gzip/сжатие payload — преждевременно.

## Связанные задачи

- **ID-100** «Race condition при параллельных ревью одного деплоя» — тот же `_sessions`;
  thread-safety проектируется там.
- **ID-101** «publish_review теряет находки при частичном сбое GitHub API» — соседняя устойчивость
  publish.
- **ID-108** «E2E integration-тест полного цикла» — добавить ветку «рестарт между стадиями»; не
  блокирует PRI-99.
- **ID-104** «CLI headless-режим» — главный потребитель future-proof-расширения.
