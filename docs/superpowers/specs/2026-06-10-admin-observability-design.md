# Дизайн: админка наблюдаемости агента ревью

## Context

Агент `rag_for_git` ревьюит PR из CLI (`reviewer review owner/repo N`). Сейчас о прогонах
**ничего не сохраняется**: `UsageLog` (токены + стоимость по этапам) печатается в консоль и
исчезает; `VerdictLog` — опциональный per-finding JSONL без токенов/стоимости/времени. Нет
способа посмотреть историю: как работал агент, сколько стоил, что находил, насколько точен.

Цель — небольшая веб-админка наблюдаемости: персистить каждый прогон ревью и показывать
историю, стоимость и находки с drill-down.

## Решения (утверждены)

- **Scope:** полная observability — записи уровня прогона + уровня находки.
- **Стек:** FastAPI (JSON-API) + React/Vite SPA (Recharts для графиков).
- **Режим:** только история (записи появляются после завершения прогона). Live-стрим
  прогресса — вне области v1.
- **Авторизация:** нет (локальный/внутренний инструмент).
- **Хранилище:** переиспользуем Postgres (`PG_DSN`), 2 новые таблицы.

## Модель данных (Postgres)

`reviewer/web/schema.sql` (применяется идемпотентно при старте `serve` и при записи истории).

**`review_runs`** — один прогон:
- `id` BIGSERIAL PK, `created_at` timestamptz
- `repo` text, `pr_number` int, `base_sha` text, `head_sha` text
- `model` text, `model_verify` text, `dry_run` bool
- `started_at` / `finished_at` timestamptz, `duration_ms` int
- `status` text — `ok` | `error` | `draft_skip`
- `files_reviewed` int, `files_skipped` int, `files_failed` int
- `findings_analyzed` int, `findings_kept` int, `verify_rejected` int
- `comments_inline` int, `comments_summary` int
- `usage` jsonb — `{stage: {calls, input_tokens, output_tokens, cache_read_tokens, cost}}`
- `total_cost` numeric
- `error_text` text NULL

**`review_findings`** — на находку (только прошедшие в итог + опубликованные):
- `id` BIGSERIAL PK, `run_id` BIGINT FK → review_runs(id) ON DELETE CASCADE
- `file` text, `line` int NULL, `category` text, `severity` text, `confidence` real
- `is_real` bool, `published` bool, `inline` bool, `fingerprint` text
- `message` text (обрезан до 500)

## Захват данных

В конце `reviewer review` (после `build_graph(...).invoke(...)`), в `cli.py`:
- тайминги: засечь `started/finished` вокруг invoke; `status` из исхода (draft-skip — до invoke);
- метаданные: repo, pr, base/head sha, модели, dry_run;
- счётчики из финального `state`: `verified` (kept), `inline_comments`, `failed_units`,
  `skipped_paths`; `findings_analyzed` = уник. fingerprint из `state["findings"]`;
  `verify_rejected` = analyzed − kept;
- usage по этапам: новый метод `UsageLog.snapshot() -> dict[stage, {...}]` (структура, которую
  `report()` уже считает) + `total_cost`;
- находки: из `state["verified"]`, флаги `published`/`inline` сопоставляются с `inline_comments`
  (по fingerprint) — что ушло inline, что в сводку.

Запись делает `ReviewHistory.record_run(run_dict, findings_list)` — **fail-soft**: любой сбой
БД ловится и логируется, ревью не падает. Гейт `REVIEW_HISTORY` (Settings, дефолт `true`).

## Бэкенд — `reviewer/web/`

- `history.py` — `ReviewHistory(pg_dsn)`: `init_schema()`, `record_run(...)`, `list_runs(filters, page)`,
  `get_run(id)` (+ находки), `stats()` (агрегаты). Изолирован за своим интерфейсом, как другие сторы.
- `api.py` — FastAPI-роуты:
  - `GET /api/runs?repo=&status=&limit=&offset=` → список (пагинация).
  - `GET /api/runs/{id}` → прогон + массив находок.
  - `GET /api/stats?days=` → агрегаты: суммарная стоимость, $/прогон, число прогонов/находок,
    % отсева verify, ряды «стоимость во времени» и «прогоны во времени», находки по
    категориям/severity.
- `app.py` — фабрика FastAPI: монтирует `/api`, отдаёт собранный SPA (`web/frontend/dist`) как
  статику на `/` (SPA-fallback на `index.html`). CORS для dev (Vite на :5173).
- CLI: команда `reviewer serve [--host 127.0.0.1] [--port 8000]` (uvicorn).
- Зависимости: extra `[web]` = `fastapi`, `uvicorn[standard]` (ядро остаётся лёгким).

## Фронт — `web/frontend/` (React + Vite + Recharts)

- **Dashboard** — KPI-карточки (прогонов, суммарно $, $/прогон, находок, % отсева verify) +
  графики: стоимость во времени, прогоны во времени, находки по категориям, сплит по severity.
- **Runs** — таблица (время, repo#PR, модель, файлы, находки, inline/сводка, длительность, $,
  статус) с сортировкой/фильтрами; строка → детали.
- **RunDetail** — шапка (ссылка на PR на GitHub, base/head sha, модель, статус, длительность) +
  разбивка токенов/стоимости по этапам + таблица находок (file:line, категория/severity,
  confidence, вердикт is_real, inline/сводка, текст) + блок ошибок/пропущенных файлов.
- Лёгкий клиент API (`fetch`), роутинг (react-router), минимальный стиль (без тяжёлого UI-кита).

## Структура

```
reviewer/web/        __init__.py · app.py · api.py · history.py · schema.sql
reviewer/entrypoints/cli.py   ← + команда serve, + хук записи истории в review()
reviewer/llm/usage.py         ← + UsageLog.snapshot()
reviewer/config/settings.py   ← + review_history: bool = True
web/frontend/        package.json · vite.config.ts · index.html · src/ (api, App, Dashboard, Runs, RunDetail)
docs/superpowers/specs/2026-06-10-admin-observability-design.md
```

## Обработка ошибок

- Запись истории — fail-soft (ревью важнее лога).
- API — корректные коды (404 на неизвестный run, 500 с логом на сбой БД).
- Фронт — состояния loading/empty/error на каждой странице.
- `serve` без БД — понятная ошибка с подсказкой про `docker compose up`.

## Тестирование

- Юнит: `ReviewHistory` (запись прогона+находок, чтение, агрегаты) — integration-маркер для
  реального Postgres; `UsageLog.snapshot()` на фейках.
- API: FastAPI `TestClient` поверх мок-стора (формы ответов, фильтры, 404).
- Fail-soft: запись истории при «битом» сторе не бросает.
- Фронт: smoke-сборка (`vite build`) — без тяжёлых FE-тестов в v1.

## Вне области (v1)

- Live-мониторинг прогресса прогона (стрим/websocket).
- Авторизация/мультипользовательность.
- Хранение отклонённых verify-находок целиком (храним счётчик `verify_rejected`; полный
  drill-down по отклонённым — возможный follow-up).
- Ретеншн/чистка старых прогонов.
