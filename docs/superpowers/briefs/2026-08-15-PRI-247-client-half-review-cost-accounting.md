# Brief — PRI-247 Достроить клиентскую половину учёта расхода ревью (usage/total_cost/review_steps)
https://ru.yougile.com/team/686c049c8af8/#PRI-247

## Task
- Ключ ID-301 (алиас PRI-247), статус доски «Движок (reviewer CLI/MCP)».
- Проблема: серверная половина PRI-209 (`publish_review` уже принимает model/usage/total_cost/steps/started_at) работает, но у всех 17 реальных прогонов review-pr usage/total_cost NULL, model вырожден в `claude-code`, review_steps пуст — клиент (скилл review-pr) метаданные не отдаёт.
- 7 шагов: 1) спайк — получает ли PreToolUse-хук `transcript_path`; 2) вынести агрегацию транскрипта из `brief_cost.py` в общий stdlib-модуль хуков без изменения поведения solve-task; 3) хук на `publish_review`, считающий расход окна ревью в sidecar-JSON по детерминированному пути от repo+PR; 4) `publish_review` читает sidecar и заполняет model/usage/total_cost (явные аргументы клиента приоритетнее); 5) серверный учёт review_steps по каждому тул-вызову PR-сессии (стадия/имя/байты payload), независимый от хуков; 6) разрез расхода по стадиям в веб-админке; 7) пересборка codex-манифестов плагина.
- Критерии приёмки: usage/total_cost непусты и model реальна; review_steps есть даже без хуков; отсутствие/сбой хука не ломает ревью (fail-open); юнит-тесты на парсер транскрипта и sidecar, поведение brief_cost для solve-task не меняется; разрез по стадиям виден в админке; **total_cost — взвешивание бакетов, не сложение токенов**.

## Related work
- ID-300 (PRI-246, done, PR #197) — спайк, доказавший парсибельность транскрипта тем же паттерном, что и `brief_cost.py`; дал формулу взвешивания токенов (input≈×1, output≈×5, cache_write≈×1.25, cache_read≈×0.1) и предупредил, что sidechain-расход у per-file субагентов идёт отдельным процессом/транскриптом — для review-pr точку съёма надо перепроверить на реальном прогоне, а не полагаться на вывод по solve-task.
- ID-304 (done, PR #198) — офлайн-харнесс метрик solve-task: соседний прецедент трейс-агрегации и её тестирования, полезен как образец структуры тестов.
- ID-302 (открыта, «Плагин/агент (скилы)») — параллельная инициатива по переносу preflight/сбора контекста solve-task на server-side тул (`prepare_task_context`); не пересекается по коду, но тот же паттерн «убрать логику из промпта скилла в детерминированный серверный слой».
(dropped 1: ID-305 — про `implementations`/`family` в графе ретрива, к учёту расхода не относится)

## Subsystems
- reviewer/mcp — `MCPReviewService` держит `_Session` (candidates/verdicts/steps, `_MAX_SESSION_STEPS=1000`); `publish_review` уже принимает model/usage/total_cost/steps/started_at (PRI-209) и пишет их в `_RunMetadata` → `_record_history`; `_invoke_tool` пересоздаёт `make_tools` на каждый вызов — вероятная точка для серверного учёта review_steps по каждому тул-вызову (шаг 5).
- reviewer/web — `ReviewHistory.record_run` пишет run+findings+steps одной транзакцией в `review_runs`/`review_findings`/`review_steps` (схема уже содержит `stage`/`unit`/`seq`/`kind`/`name`/`text`/`tool_calls`/`tokens`/`cost` на шаг); admin API отдаёт `/api/runs`, `/api/runs/{id}/trace` — сюда добавлять разрез по стадиям (шаг 6).
- plugin/hooks — `brief_cost.py`/`brief_guard.py`/`brief_post_write.py`: паттерн stdlib-only fail-open хука, читающего `transcript_path` из payload; `reviewer_defect.py` — образец self-contained hook без зависимости от пакета `reviewer` (системный python3), с продублированным словарём, сверяемым guard-тестом — модель для общего модуля агрегации и для нового хука на publish_review.
- reviewer/metrics/brief_quality — `briefs.py::TokenBlock`/`parse_token_block` — второй существующий потребитель формата блока токенов; учитывать при решении, что именно выносить в общий модуль.

## Relevant code
- plugin/hooks/hooks.json:1-25 — сейчас зарегистрирован только `PostToolUse` (matcher `Write` → `brief_post_write.py`; matcher `mcp__reviewer__.*|mcp__plugin_rag-reviewer_reviewer__.*` → `reviewer_defect.py`). **PreToolUse отсутствует вовсе** — шаг 1 (спайк) не имеет прецедента в этом репо и должен ответить эмпирически, получает ли PreToolUse-хук `transcript_path` (PostToolUse точно получает — см. ниже).
- plugin/hooks/brief_cost.py:124-155 (`aggregate_usage`), :46-67 (`render_block`), :257-291 (`run`) — текущая агрегация 4 бакетов (fresh_in/output/cache_write/cache_read) по model с разделением main/sidechain; прямой кандидат на вынос в общий stdlib-модуль (шаг 2), поведение solve-task обязано остаться идентичным.
- plugin/hooks/brief_guard.py:238-275 (`_read_fresh_transcript`) и :299-340 (`run`) — второй самостоятельный parser транскрипта с retry по `tool_use_id` и подтверждением, что `payload.get("transcript_path")` доступен в PostToolUse.
- plugin/hooks/reviewer_defect.py — эталон self-contained/stdlib-only хука с матчером на оба префикса имени тула (`mcp__reviewer__.*` и `mcp__plugin_rag-reviewer_reviewer__.*`); новый хук на `publish_review` естественно строить по этому же паттерну матчинга.
- reviewer/mcp/service.py:2907-3077 (`publish_review`), :3036-3043 (`_RunMetadata`) — точка, где нужно прочитать sidecar JSON и слить с явными аргументами клиента (шаг 4, приоритет — явным); сигнатура уже принимает все нужные поля.
- reviewer/mcp/service.py:195-263 (`prepare_review`) — здесь резервируется ключ сессии по (repo, pr); тот же ключ должен детерминированно давать путь sidecar-файла (шаг 3: «путь, производный от repo и номера PR»).
- reviewer/web/history.py:79-172 (`init_schema`/`record_run`/`_SCHEMA`, `step_sql`) — `review_steps` уже содержит нужные колонки; для шага 5 источник новых steps — вероятно `_invoke_tool` в `service.py`, независимо от хуков клиента.
- reviewer/entrypoints/mcp_server.py:289-321 — тонкая FastMCP-обёртка `publish_review`, транзитом прокидывающая параметры; при появлении sidecar-режима, вероятно, не меняется (сигнатура уже полная).
- plugin/skills/review-pr/SKILL.md:127 (по тексту описания задачи) — текущая инструкция «If the CLI provides model/usage/cost metadata, pass them» предполагает, что CLI сам отдаёт метаданные, чего не происходит; при вводе sidecar-хука эта строка скилла, вероятно, требует правки.
- eval/pri246_solve_task_cost.py, eval/pri246_report.md (§1, §3.1) — артефакты спайка PRI-246 с формулой взвешивания бакетов и данными по 28 брифам — источник weighting-констант для total_cost.
(dropped 0)

## Test exemplars
- tests/hooks/test_brief_cost.py:101-141 (`test_aggregate_usage_splits_main_and_sidechain`, `test_aggregate_usage_counts_sidechain_solve_task_tokens`) — прямые юнит-тесты `aggregate_usage` (main/sidechain split, multi-model); паттерн для тестов общего модуля после выноса (критерий 4).
- tests/hooks/test_brief_cost.py:196-209 (`test_run_end_to_end_writes_block`) — end-to-end тест `run()` с фикстурным `transcript.jsonl` и `tmp_path`; паттерн для теста нового хука на publish_review.
- tests/mcp (кластер «Тесты MCP-сервера reviewer-mcp», 24 unit-теста) — покрывает `publish_review` gate/dedup/assemble/history; сюда лягут тесты на чтение sidecar с приоритетом явных клиентских аргументов и на fail-open при отсутствующем/битом sidecar.
(dropped 0)

## Constraints / open questions
- Хуки исполняются системным python3 без установленного пакета `reviewer` → общий модуль агрегации транскрипта обязан остаться stdlib-only и self-contained (как `reviewer_defect.py`), не импортом из `reviewer/`.
- В hooks.json сейчас нет ни одного PreToolUse-хука — ответ на шаг 1 (спайк) придётся получить эмпирически, без опоры на прецедент в этом репо.
- **total_cost обязан считаться взвешиванием бакетов** (input≈×1, output≈×5, cache_write≈×1.25, cache_read≈×0.1 по спайку PRI-246), не сложением токенов — простая сумма завышает стоимость в ~4.1 раза и делает разрез по стадиям вводящим в заблуждение.
- Sidechain-расход per-file субагентов review-pr вероятно идёт отдельным процессом/транскриптом (как для solve-task в PRI-246) — агрегация по одному `transcript_path` рискует не увидеть основную часть стоимости ревью; нужно перепроверить на реальном прогоне review-pr, а не полагаться на вывод по solve-task.
- Отсутствие/сбой хука не должно ломать ревью (fail-open, критерий 3): `publish_review` обязан работать и без sidecar, поля расхода просто остаются пустыми.
- Шаг 5 (серверный учёт review_steps) явно должен работать «в любом CLI», то есть независимо от хуков — источник данных, вероятно, `_invoke_tool` в `MCPReviewService`, а не клиентский payload; это отдельная от sidecar-хука реализация с отдельными тестами.
- Любая правка содержимого `plugin/` меняет codex payload-digest → обязателен прогон `update_codex_plugin_manifest.py` перед коммитом (шаг 7), иначе install-тесты покраснеют.
- `plugin/skills/review-pr/SKILL.md:127`, вероятно, требует правки одновременно с сервером; по конвенции репозитория такие изменения синхронно отражаются в README.md/README.ru.md, если меняют наблюдаемое поведение.

---
Собран на: mid (Sonnet), сборка: subagent
