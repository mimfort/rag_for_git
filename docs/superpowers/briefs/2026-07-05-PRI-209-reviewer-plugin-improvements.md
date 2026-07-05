# Brief — PRI-209 [reviewer] Улучшения плагина после анализа PRI-208 и трейса в БД
https://ru.yougile.com/team/686c049c8af8/#PRI-209

## Task
Задача **реальна** — конкретный бэклог-пункт с воспроизводимыми дефектами в БД review history.
Закрыть пробелы в наблюдаемости/учёте плагина reviewer после внедрения PRI-208:
(1) `review_runs` пишутся с `duration_ms=0`, `model="claude-code"`, `usage=None`, `total_cost=None`, `steps=None`;
(2) `plugin/hooks/brief_cost.py` не видит sidechain-токены сабагента, который теперь собирает бриф в PRI-208;
(3) `solve-task` Step 1.5 требует вопроса о tier модели — в auto permission mode нужен silent fallback;
(4) в БД отсутствуют прогоны для свежих PR #93–#97, нужен guard/alert на зазор.

## Related work
- **PRI-208** (done, PR #97) — выбор модели для сборки брифа в solve-task: источник sidechain-проблемы; прецедент cross-CLI model tier и guard-тестов.
- **PRI-131** (Движок) — Why-trace (грундинг) на каждом комментарии: смежное направление «trace»; не дублировать, но не конфликтовать с `review_steps`.
- **PRI-164** (done) — solve-task brief hygiene: резолв subtask-criteria и дедуп linked vs similar; паттерн работы со skill-артефактами.
- (dropped 5: ID-98 auto-config allowedTools, ID-115 review-local, ID-203 RAG за пределами брифа, ID-147 tool-economy, ID-161 subsystem summaries — не напрямую про observability/учёт токенов и трейс.)

## Subsystems
- `plugin/hooks` — хук `brief_cost` считает токены solve-task, но фильтрует `isSidechain=True`.
- `reviewer/mcp` — `MCPReviewService` захардкоживает метаданные прогона в `_record_history` и не пишет `review_steps`.
- `reviewer/web` — `ReviewHistory.record_run` уже поддерживает steps/usage/cost, но caller даёт заглушки; API trace готов.
- `plugin/skills/solve-task` — Step 1.5 выбора модели не обрабатывает auto permission mode.
- (dropped 0)

## Relevant code
- `reviewer/mcp/service.py:1050` — `model: "claude-code"` захардкожен; нужно реальное имя модели (параметр/мета от клиента или детекция).
- `reviewer/mcp/service.py:1053-1055` — `started_at == finished_at == now`, `duration_ms: 0`; нужно фиксировать реальное время прогона.
- `reviewer/mcp/service.py:1071-1072` — `usage: None`, `total_cost: None`; нужен механизм получения usage/cost от клиента/сабагентов.
- `reviewer/mcp/service.py:1079` — `history.record_run(..., steps=None)`; нужно собирать и передавать серверные `review_steps`.
- `reviewer/web/history.py:73-161` — `ReviewHistory.record_run` уже поддерживает steps/usage/cost; только caller не заполняет.
- `reviewer/web/schema.sql:53-67` — схема `review_steps` с полями `stage`, `unit`, `seq`, `kind`, `name`, `text`, `tool_calls`, `tokens`, `cost`.
- `plugin/hooks/brief_cost.py:88-97` — `find_window_start` ищет маркеры solve-task в user-сообщениях.
- `plugin/hooks/brief_cost.py:100-115` — `aggregate_usage` пропускает `isSidechain=True`; токены сабагента-брифа теряются.
- `plugin/skills/solve-task/SKILL.md:76-83` — Step 1.5 выбора модели; нужен fail-open fallback на mid tier в auto mode.
- `reviewer/services/review_service.py:143-154` — `_ensure_history` создаёт `ReviewHistory`; точка инициализации прогона.
- (dropped 0)

## Test exemplars
- `tests/web/test_history.py:258-291` — `test_record_run_with_steps_and_get_trace`: паттерн записи/чтения steps.
- `tests/web/test_history.py:22-48` — `_sample_run` показывает ожидаемые поля `duration_ms`/`model`/`usage`.
- `tests/mcp/test_publish.py:129-170` — фейк `ReviewHistory` и паттерн проверки `record_run` в публикации.
- `tests/hooks/test_brief_cost.py:90-107` — `test_aggregate_usage_sums_per_model_skips_sidechain`: текущее поведение; нужен новый тест на учёт (или явную пометку) sidechain.
- `tests/skills/test_solve_task_brief.py` (guard-тесты solve-task) — для auto-mode-fallback нужна новая anchor-фраза.
- (dropped 0)

## Constraints / open questions
- **Auto permission mode активен** — solve-task Step 1.5 должен молча выбирать mid tier, не спрашивая.
- **Existing artifact** — предыдущий бриф `docs/superpowers/briefs/2026-07-04-PRI-209-reviewer-plugin-improvements.md` перезаписан.
- **PRI-208 в PR #97** — при реализации нужно будет вмержить/ребейзнуться на `dev` или работать поверх PR #97, чтобы sidechain-логика была актуальна.
- **DB trace пуст**: все 16 прогонов имеют `duration_ms=0`, `usage=None`, `steps=None`; нет данных за PR #93–#97.
- **Sidechain-токены**: формат JSONL-транскрипта для сабагентов неизвестен — нужно исследовать, прежде чем учитывать их в `brief_cost`; возможно, ограничимся явной пометкой/документированием ограничения.
- **Связь с PRI-131**: why-trace для комментариев — отдельная задача; `review_steps` здесь — серверный trace этапов, не grounding отдельной находки.
- **Model/usage/cost**: LLM-вызовы происходят в клиенте (Claude Code), поэтому сервер не знает реальных значений; нужен контракт передачи мета-информации от клиента к `publish_review` (или отдельный MCP-тул).

Собран на: mid tier (session model), режим: inline
