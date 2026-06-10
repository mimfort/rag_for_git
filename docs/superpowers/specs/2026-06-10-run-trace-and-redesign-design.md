# Дизайн: трейс прогона агента + редизайн админки

## Context

Веб-админка наблюдаемости показывает итоги прогонов (стоимость, находки), но **не видно,
как агент работал внутри**: какие инструменты он выбирал, как сработал RAG, что вернул каждый
LLM-вызов. Сейчас этот трейс нигде не сохраняется — tool-loop (`analyzer.py::_run_tool_loop`)
гоняет `llm.invoke` → `tool.invoke` и выбрасывает промежуточные сообщения.

Цель: (1) захватывать и сохранять пошаговый трейс каждого прогона и показывать его в админке;
(2) полный визуальный редизайн админки в стиле «тёмный премиум-дашборд» (Linear/Vercel).

## Часть 1 — Захват трейса

### `TraceLog` (новый коллектор, потокобезопасный)
По образцу `UsageLog` (analyze-узлы LangGraph идут параллельно → нужен лок и общий счётчик `seq`).
Методы:
- `record_prompt(stage, unit, text)` — стартовый промпт юнита (контекст файла + дифф), один раз.
- `record_llm_call(stage, unit, ai_message)` — из `AIMessage`: текст-рассуждение, `tool_calls`
  (имя+args каждого выбранного тула), токены и стоимость (`usage_metadata` / `response_metadata`).
- `record_tool_call(stage, unit, name, args, result)` — вызов инструмента и его результат.
- `snapshot() -> list[dict]` — упорядоченный по `seq` список шагов для персиста.
Капы: `text` обрезается (~8 КБ/шаг). Никогда не бросает (как `UsageLog`).

### Хук в `analyzer.py`
`_run_tool_loop(..., trace=None, unit="")` — рядом с существующим хуком `usage.add(...)`:
- после `ai = llm.invoke(messages)` → `trace.record_llm_call(stage, unit, ai)`;
- после `tool.invoke(...)` → `trace.record_tool_call(stage, unit, call["name"], call["args"], result)`.
Вызывающие передают `trace` (из `Deps.trace`) и `unit`:
- **analyze** — `stage="analyze"`, `unit=unit.path`; перед циклом `record_prompt`.
- **verify** (`_verify_one`) — `stage="verify"`, `unit=f"{f.file}:{f.line}"`.
- **synthesize** — `stage="synthesize"`, `unit="(синтез)"`.
`Deps` получает поле `trace: object = None`. Гейт `REVIEW_TRACE` (Settings, дефолт `true`).

### Хранение — таблица `review_steps`
```
id BIGSERIAL PK · run_id BIGINT FK→review_runs(id) ON DELETE CASCADE
stage TEXT (analyze|verify|synthesize) · unit TEXT · seq INT
kind TEXT (prompt|llm_call|tool_call) · name TEXT NULL (имя тула)
text TEXT (обрезан) · tool_calls JSONB NULL ([{name,args}] для llm_call)
tokens INT · cost NUMERIC · created_at TIMESTAMPTZ
INDEX (run_id, seq)
```
Запись в конце `reviewer review` (расширить `ReviewHistory.record_run(run, findings, steps=None)`
— вставка шагов с `run_id`). Fail-soft. Объём: PR #14 ≈ 60 шагов ≈ ~0.5 МБ — приемлемо.

### API
`GET /api/runs/{id}/trace` → шаги прогона, упорядоченные по `seq` (грузится по требованию,
отдельно от `/api/runs/{id}`, т.к. трейс крупный). `ReviewHistory.get_trace(run_id)`.

## Часть 2 — Редизайн фронта (тёмный премиум-дашборд)

Полный визуальный редизайн через **frontend-design** (избегаем «генерик-AI» вида):
дизайн-система с CSS-переменными, глубокий фон, glass-карточки, мягкие градиенты, неоновые
акценты, выверенная типографика и сетки. Графики recharts — градиентные area-заливки,
кастомные тултипы, скруглённые бары. Передизайн **Dashboard**, **Runs**, **RunDetail**.

**RunDetail → вкладка «Трейс»** (грузит `/api/runs/{id}/trace`): таймлайн по этапам
(analyze → verify → synthesize), каждый юнит — аккордеон, внутри упорядоченные шаги:
- **LLM-вызов**: рассуждение (сворачиваемое) + чипы «выбрал тулы: `search_code(query=…)`,
  `find_callers(node_id=…)`» + бейдж токенов/стоимости.
- **Tool-результат**: имя + аргументы + результат (сворачиваемый). Для `search_code`/RAG —
  результат парсится в список извлечённых чанков (`node_id`, `path:строки`).
- Внизу юнита — итоговые findings.
Состояния loading/empty (для прогонов без трейса — старые прогоны / `REVIEW_TRACE=false`).

## Часть 3 — Docker
Пересобрать образ `web` (`docker compose up -d --build web`) — новый фронт-бандл попадёт в образ.

## Тестирование
- Юнит: `TraceLog` (запись шагов, потокобезопасность, капы, snapshot) на фейках.
- Юнит/интеграция: `ReviewHistory.record_run` со steps + `get_trace` (integration-маркер для PG).
- API: `GET /api/runs/{id}/trace` через `TestClient` с мок-стором.
- Сквозной: реальный `reviewer review … --dry-run` → проверить, что в `review_steps`
  появились llm_call/tool_call с непустым trace.
- Фронт: `npm run build` (tsc+vite) без ошибок.

## Вне области (v1)
- Трейсы только для **новых** прогонов (инструментация forward-only; у текущих 5 трейса нет).
- RAG отображается из текста результата `search_code` (node_id/path); отдельные скоры
  ретрива/реранка не захватываются — возможный follow-up.
- Полные входные message-блобы не храним (избыточно/огромно) — храним вывод каждого вызова
  (рассуждение + выбранные тулы) и результаты тулов; стартовый промпт — один раз на юнит.
