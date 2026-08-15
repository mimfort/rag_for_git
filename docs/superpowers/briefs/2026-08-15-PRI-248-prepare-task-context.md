# Brief — PRI-248 solve-task: свернуть preflight и сбор контекста в prepare_task_context, разгрузить промпт скилла
https://ru.yougile.com/team/686c049c8af8/#PRI-248

## Task
plugin/skills/solve-task/SKILL.md — 422 строки, крупнейший файл плагина, грузится в контекст оркестратора целиком при каждом вызове. Шаги 0.1–0.4 (drift, sync_board, теплота сводок) и Шаг 3 (subsystem prior, get_task_context, search_tasks, search_codebase, тест-образцы, графовые расширения) — 8–12 тул-раундов без решений, требующих LLM. Спроектировать/реализовать session-less тул `prepare_task_context(repo, path, branch, key)`, отдающий единым payload: drift, теплоту сводок, разрешённый `task_board`, саму задачу, linked+similar задачи, релевантный код, тест-образцы — с сохранением всей fail-open семантики (недоступная доска/Neo4j/индекс → частичный payload с помеченными пробелами, не ошибка). LLM оставляет только relevance-фильтр, сборку брифа и решения по режимам. Ужать SKILL.md по образцу review-pr (`references/` + `<!-- include: ... -->`), перевести guard-тесты (`test_solve_task_brief.py`, `test_preflight_guardrail.py`, `test_solve_task_modes.py`) на `assemble()` вместо грепа сырого SKILL.md, пересобрать codex-манифесты, обновить оба README при изменении потока.

**Критерии приёмки:** (1) путь до брифа — существенно меньше тул-раундов; (2) guard-тесты проверяют собранный текст промпта, не сырой SKILL.md; (3) поведение во всех сбойных сценариях (доска выключена, Postgres/Neo4j лежат, индекс устарел, сводки не построены) не меняется, скил не падает; (4) новый тул покрыт юнит-тестами без живых внешних сервисов, сетевые пути — под `integration`; (5) codex-манифесты пересобраны, install-тесты зелёные; (6) эффект подтверждён замером во взвешенных единицах (до/после), не сокращением числа раундов на глаз.

## Related work
- PRI-140 — прецедент того же паттерна консолидации: `sync_board` как server-side ETL, LLM дёргает один тул без payload вместо построчного обхода доски; дизайн-референс для `prepare_task_context`.
- PRI-141 — реализация текущего Preflight (Step 0: drift-проверка через `reviewer status --json`, инкрементальный `sync_board`, warn-баннер для `ask`) — именно то, что PRI-248 сворачивает в один серверный вызов.
- PRI-246 — спайк, прямая предпосылка: измерил цену solve-task (медиана 2.81M токенов/задачу, взвешенно ≈654K), но не разбил её на подшаги, и явно рекомендует делать **сначала** дешёвую step-level инструментацию тем же паттерном, что `brief_cost.py` (сколько cache-write на каждый под-шаг), **затем** дизайн `prepare_task_context` — иначе консолидация проектируется вслепую.
- PRI-247 — сестринская задача, уже сделана: формула взвешивания бакетов (`fresh_in×1, output×5, cache_write×1.25, cache_read×0.1`) и паттерн «хук парсит транскрипт → sidecar JSON» — переиспользовать для замера критерия приёмки 6.
- ID-304 (офлайн-харнесс метрик solve-task: цена/качество ретрива/тренд, done, PR #198) — измерительный харнесс, потенциально переиспользуем для «до/после».
- PRI-272 — упомянута в описании как часть той же линии («убрать дамп сводок из preflight»), но в сторе reviewer отсутствует (`get_task` вернул `null`) — не удалось подтвердить содержание напрямую.
(dropped 4: ID-138 — формат выдачи `search_codebase` (дедуп/номера строк), уже отгружен и не про консолидацию тулов; ID-198 — баннер «грязное рабочее дерево», не пересекается с preflight-механикой; ID-163 — персист брифа в артефакт, уже реализован и описан в текущем SKILL.md; ID-208 — выбор модели для сборки брифа, ортогонально теме этой задачи)

## Subsystems
- reviewer/mcp — сервисный слой MCP: `MCPReviewService` держит session-bound и session-less тулы, `service.py`/`mcp_server.py` — куда встраивается новый тул.
- reviewer/services — `ReviewService.prepare` и статус/синк-сервисы — образец «одним вызовом собрать весь контекст», уже применённый для PR-ревью.
- reviewer/tasks — жизненный цикл задач досок: server-side ETL (`sync_board`), нормализация в `TaskBrief`, граф `:Task` — сюда ложится агрегация задачи+linked+similar.
- reviewer/policy — `ContextLimits`/`ReviewPolicy`, лимиты retrieval-тулов (`floor`/`ceiling` для codebase/tasks/graph) — вероятно нужны новому тулу для тех же адаптивных колпаков.
- tests/skills — контрактные тесты assembled skill prompts, `_common`-блоков, store-first task workflows — сюда переносятся четыре гвард-теста задачи.

## Relevant code
- plugin/skills/solve-task/SKILL.md:21-114 — Step 0 Preflight целиком (index freshness, sync_board warm-up, summary warmth) — блок, который `prepare_task_context` должен заменить одним вызовом.
- plugin/skills/solve-task/SKILL.md:145-237 — Steps 2-3 (identify task + gather context: `get_task`, `get_subsystem_summaries`, `get_task_context`, `search_tasks`, `search_codebase`, графовые тулы) — вторая половина раундов для консолидации.
- reviewer/mcp/service.py#MCPReviewService (179-299) и `prepare_review` (212-280) — образец для нового агрегирующего session-less тула: reservation-before-build, try/except с fail-soft откатом, единый dict-payload на выходе.
- reviewer/entrypoints/mcp_server.py#create_server (18-590), регистрация `prepare_review` (26-31) — где физически регистрируются MCP-тулы; сюда добавляется `prepare_task_context`.
- reviewer/services/status.py#BranchStatus (16-25) — dataclass с полями `drift`/`summaries`, уже отдаваемыми `reviewer status --json`; вероятный источник для полей drift/summary-warmth в payload нового тула.
- reviewer/config/task_board.py#TaskBoardConfig (35-68) — форма разрешённого `task_board` (`board_type`/`project`/`key_pattern`/`create_target`/`done_target`/`options`), которую `prepare_task_context` обязан вернуть как есть (контракт «resolve once» из Step 1).
- tests/skills/test_assembled_prompts.py#assemble (12-25) — резолвер include-маркеров, на который задача требует перевести гвард-тесты; нерекурсивный, путь маркера относительно `plugin/skills/`.
(dropped 0: все найденные фрагменты по прямым запросам сохранены)

## Test exemplars
- tests/skills/test_preflight_guardrail.py#test_solve_task_has_preflight (14-20) — сейчас грепает сырой SKILL.md на "Preflight"/"drift"/"sync_board("/"rag-reviewer:sync-codebase" — прямая цель миграции на `assemble()`.
- tests/skills/test_solve_task_brief.py#test_solve_task_brief_spec_present (15-21) и #test_solve_task_uses_only_generic_board_metadata (135-140) — raw-text ассерты на скелет брифа и generic board-метаданные (запрещённые токены вроде `yougile`/`api_key`) — тот же файл, что назван в задаче для переноса.
- tests/skills/test_ask_uses_summaries.py#test_solve_task_has_subsystems_brief_section / test_solve_task_marks_summary_prior_only (41-50) — raw-text проверки секции `## Subsystems` и правила «grounding только из search_codebase» — потребуют эквивалентного покрытия после рефакторинга.
- tests/skills/test_review_pr_store_first.py#test_solve_task_resolves_board_once_before_preflight_sync (39-48) — regex-заякоренная структурная проверка «Resolve task_board exactly once … до sync_board(» — хрупкая к переносу текста в references/, тихо сломается при реструктуризации.
- tests/skills/test_assembled_prompts.py#test_solve_task_assembled_has_branch_and_tools (90-93) — уже существующий пример теста через `assemble("solve-task/SKILL.md")` — образец, по которому переписывать остальные три файла.
(dropped 0)

## Constraints / open questions
- **Порядок из PRI-246 обязателен**: спайк прямо предписывает сначала step-level инструментацию расхода по подшагам solve-task (тем же паттерном, что `brief_cost.py`), и только затем — дизайн `prepare_task_context`. В найденном контексте отдельного артефакта такой пошаговой инструментации не обнаружено — возможно, это первый под-шаг самой задачи PRI-248, а не отдельно закрытая предпосылка.
- Критерий приёмки 6 требует замера «во взвешенных единицах» — переиспользовать формулу PRI-247 (`fresh_in×1, output×5, cache_write×1.25, cache_read×0.1`), а не наивную сумму токенов, иначе замер до/после будет систематически неверным (та же ошибка, что спайк PRI-246 нашёл в сыром recall).
- PRI-272 (продолжение той же линии, «убрать дамп сводок из preflight») упомянута в описании, но отсутствует в сторе reviewer — не удалось проверить, что именно она меняла и не пересекается ли с этой задачей.
- Граф (implementations/callers/related_symbols/family) не запрашивался: задача — про новый MCP-тул и рефакторинг промпта, не про роль в конкретной OO/registry-иерархии; центральных символов для расширения не выявлено.
- `get_pr_diff` по связанным PR не запрашивался: все прямые связи (PRI-140/141/246/247) — уже смерженные предпосылки, их диффы — это готовые паттерны (ETL-тул, weighted cost), а не прямая реализация целевого `prepare_task_context`.
- Board-состояние: доска синхронизирована заранее (109 задач, 0 изменений), `sync_board`/`index_task` не вызывались согласно инструкции.

---
Собран на: mid (claude-sonnet-5), сборка: subagent
