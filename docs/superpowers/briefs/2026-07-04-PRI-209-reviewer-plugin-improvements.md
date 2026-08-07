# Brief — PRI-209 [reviewer] Улучшения плагина после анализа PRI-208 и трейса в БД
https://ru.yougile.com/team/686c049c8af8/#PRI-209

## Task
Проанализировать решение PRI-208 (выбор модели для сборки брифа, PR #97) и трейс в БД review history,
затем закрыть найденные пробелы в наблюдаемости/учёте плагина reviewer. Основные проблемы:
(1) в БД не пишутся шаги трейса, duration, usage, cost, model захардкожен; (2) хук brief_cost не видит
sidechain-токены сабагента, который появляется в PRI-208; (3) solve-task Step 1.5 не учитывает auto permission mode;
(4) в БД нет прогонов для свежих PR #93–#97. Задача — описать и реализовать улучшения.

## Related work
- **PRI-208** (done, PR #97) — выбор модели для сборки брифа в solve-task: источник sidechain-проблемы и
  прецедент cross-CLI model tier; мимикрировать guard-тесты и fail-open-паттерны.
- **PRI-131** (Движок) — Why-trace (грундинг) на каждом комментарии: смежное направление «trace»;
  не дублировать, но учитывать при проектировании review_steps, чтобы fingerprint не зависел от trace.
- (dropped 6: ID-199 literal search, ID-162 test exemplars, ID-187/189 sanity gates, ID-184 freshness guard,
  ID-181 blocker detection, ID-153 relevance score — другие механизмы, не напрямую влияют на observability.
  Хвост рельсы 8/30 по search_tasks не был нужен.)

## Subsystems
- `plugin/hooks` — хук `brief_cost` считает токены брифа, но пропускает sidechain-вызовы; конфликт с сабагентом PRI-208.
- `reviewer/mcp` — `MCPReviewService._record_history` захардкоживает метаданные прогона и не пишет `review_steps`.
- `reviewer/web` — `ReviewHistory` готов к steps/usage/cost, но caller не передаёт реальные данные.
- `plugin/skills/solve-task` — Step 1.5 выбора модели требует вопроса; в auto mode нужен silent fallback.
- (dropped 0)

## Relevant code
- `reviewer/mcp/service.py:1050` — `model: "claude-code"` захардкожен; нужно реальное имя модели из клиента/меты.
- `reviewer/mcp/service.py:1055` — `duration_ms: 0` вместо реального времени прогона.
- `reviewer/mcp/service.py:1071` — `usage: None`, `total_cost: None` не заполняются.
- `reviewer/mcp/service.py:1079` — `history.record_run(..., steps=None)` — шаги трейса не пишутся.
- `reviewer/web/history.py:73-161` — `ReviewHistory.record_run` уже поддерживает steps/usage/cost, но caller даёт заглушки.
- `plugin/hooks/brief_cost.py:88-97` — `find_window_start` ищет маркеры solve-task; sidechain не учитывается.
- `plugin/hooks/brief_cost.py:100-115` — `aggregate_usage` фильтрует `isSidechain=True`, поэтому токены сабагента-брифа потеряются.
- `plugin/skills/solve-task/SKILL.md:74-76` — точка вставки Step 1.5; сейчас сразу после Config идёт Identify, авто-режим не обработан.
- `reviewer/web/api.py` — эндпоинты `/api/runs/{id}/trace` готовы, но данные пусты.
- (dropped 0)

## Test exemplars
- `tests/web/test_history.py:258-291` — `test_record_run_with_steps_and_get_trace`: паттерн записи/чтения steps.
- `tests/web/test_history.py:22-48` — `_sample_run` показывает ожидаемые поля duration_ms/model/usage.
- `tests/hooks/test_brief_cost.py:90-107` — `test_aggregate_usage_sums_per_model_skips_sidechain`: текущее поведение
  пропуска sidechain; нужен новый тест на их учёт (или на явную пометку).
- `tests/skills/test_solve_task_brief.py:15-21` — guard-тесты solve-task; для auto-mode-fallback нужна новая anchor-фраза.
- (dropped 0)

## Constraints / open questions
- **Auto permission mode активен** — solve-task Step 1.5 должен молча выбирать mid tier, не спрашивая.
- **Репозиторий на коммите 4ef98d2**, PRI-208 есть в PR #97/GitHub, но не в текущем checkout — при реализации
  нужно будет вмержить/ребейзнуться на dev или работать поверх PR #97.
- **DB trace пуст**: все 16 прогонов имеют `duration_ms=0`, `usage=None`, `steps=None`; нет данных за PR #93–#97.
- **Sidechain-токены**: формат JSONL-транскрипта для сабагентов неизвестен — нужно исследовать, прежде чем
  учитывать их в `brief_cost`; возможно, ограничимся явной пометкой в брифе.
- **Связь с PRI-131**: why-trace для комментариев — отдельная задача; review_steps здесь — это серверный trace этапов,
  не grounding отдельной находки.
- **existing_artifacts**: для PRI-209 артефактов не найдено.

Собран на: session model (inline), режим: inline
