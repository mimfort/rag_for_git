# Brief — PRI-245 Снизить стоимость summarize-subsystems: skeleton-чтение, исключение тестов, батчинг файловых job
https://ru.yougile.com/team/686c049c8af8/#PRI-245

## Task
ID-299 [Бэклог]. Полный проход summarize-subsystems читает 478 .py целиком (~106k строк, из них
~68k — тесты) → >1M токенов + один субагент на файл. Три правки: (1) файловый job читает
`read_file(path, skeleton=True)` вместо harness-Read, чтобы вход совпадал с тем, что инвалидирует
sourse_hash (skeleton_hash); (2) отдельный `summary_paths.ignore` слой для кластеризации сводок
(дефолт исключает тесты), не трогая ревью-индекс; (3) батчинг файловых job'ов — один субагент на
порцию скелетов, а не на файл. Плюс два независимых механизма инвалидации существующих данных:
bump `_GENERATION` (v1→v2) и `layout_token`, включающий фильтр. Критерии: guard-тесты на скилл,
unit-тесты на фильтр, deferred==0/raced==0/completed=true на полном проходе, bootstrap после
апгрейда, старые сводки отдаются до замены bundle, prune вычищает tests/*, cap не роняет данные,
стоимость до/после зафиксирована, `.venv/bin/pytest -q` зелёный.

## Related work
- PRI-165 (ID-165, done) — ввёл source_hash от skeleton_hash (не content_hash): `reviewer/graph/summaries.py:103-110`; это структурный контракт, на котором строится skeleton-чтение шага 1 — не переизобретать, а согласовать вход job'а с уже существующим ключом свежести.
- PRI-154 (ID-154, done) — реализовал `read_file(path, skeleton=True)` (`reviewer/tools/code_tools.py:80-112`), которым шаг 1 должен пользоваться напрямую — инструмент уже готов, меняется только промпт SKILL.md.
- PRI-280 (ID-280, done) — сделал сводки инкрементальными на уровне файлов (fragments/generation stamp — `reviewer/services/summary_fragments.py`); шаг 6a (bump `_GENERATION`) продолжает этот же механизм, паттерн инвалидации уже есть, только меняется версия.
- PRI-291 (ID-291, done) — убрал эхо fingerprint из промпта file-job'а (тот же участок SKILL.md шаг 5.2); правка шага 3 (батчинг) редактирует тот же промпт-блок, нужно сохранить инвариант «job не считает и не возвращает fingerprint».
- PRI-283/PRI-287 (ID-283, ID-287, done) — пагинация + сжатый режим `list_subsystem_clusters` для больших репозиториев; тот же режим стоимости (полный проход на этом репо), которым стоит меряться при фиксации «до/после» (критерий 10).
(dropped 0 — все найденные похожие задачи по summarize-subsystems признаны прямо информирующими)

## Subsystems
- reviewer/tools — инструменты search_code/read_file/get_related_symbols и др.; read_file уже поддерживает skeleton-режим, используемый шагом 1.
- tests/skills — контрактные тесты assembled skill prompts, включая отдельно зафиксированные preflight/incremental-file-jobs/composer-grounding/optimistic-race/prune-metrics инварианты summarize-subsystems — прямой шаблон для guard-тестов шага 4.
- tests/graph — тесты кластеризации сводок, canonical layout/depth overrides, source/file fingerprints — прямой шаблон для unit-тестов фильтра кластеризации (шаг 4) и для теста инкремента layout_token (критерий 8).
(dropped 5: tests/hooks, reviewer/retrieval, reviewer/agent, plugin/hooks, tests — общая тестовая/hook-инфраструктура, не про кластеризацию сводок или skeleton-чтение)

## Relevant code
- reviewer/graph/summaries.py:103-110 (`compute_source_hash`) — source_hash уже строится от `skeleton_hash`, не `content_hash`; шаг 1 лишь согласует ЧТО job читает с тем, что УЖЕ инвалидирует сводку.
- reviewer/graph/summaries.py:113-122 (`compute_file_fingerprints`) — файловый fingerprint тоже уже skeleton-based; job'ы шага 3 группируются по этим fingerprint без изменений здесь.
- reviewer/graph/summaries.py:80-100 (`canonicalize_layout`/`compute_layout_token`) — `layout_token` = sha256 от `{default_depth, overrides}`; шаг 2/8 требует включить сюда identity нового `summary_paths.ignore` фильтра, иначе его включение/выключение не пересоберёт layout и prune не сработает (критерий 8) — точка правки.
- reviewer/graph/summaries.py:125-167 (`build_clusters`) — группирует ВСЕХ `members` без фильтра; шаг 2 либо добавляет параметр фильтра сюда, либо (предпочтительнее по слоям) отфильтровывает `members` до вызова в `_summary_state`.
- reviewer/mcp/service.py:1802-1900 (`_summary_state`) — единая точка сборки `members` (`raw = self.components.store.list_base_members(repo, branch)`) для `list_subsystem_clusters`/`get_subsystem_summary_work`/`index_subsystem_summary`; естественное место применить `summary_paths.ignore` ДО `build_clusters`, чтобы все три MCP-тула видели один и тот же отфильтрованный набор.
- reviewer/policy/policy.py:16-33,42-68,90-129 (`ReviewPolicy`) — существующий паттерн `ignore: list[str]` (paths.ignore) + `summary_cluster_depth_overrides` (dict) в `from_yaml`/`load_data`; новый `summary_paths_ignore` добавляется тем же способом (default в `from_settings`, override в `load_data`).
- .review.yml:41-46 (`paths.ignore`) и :47-56 (`summary_cluster_depth*`) — образец секции для добавления `summary_paths: ignore: [...]` в конфиг этого репозитория и в схему/докстринги.
- reviewer/config/layers.py:279,455,487,518 — эффективный merge и `reviewer config show` уже печатают `summary_cluster_depth_overrides` тем же способом; новый ключ `summary_paths_ignore`/`summary_paths.ignore` встраивается в тот же layered-resolve и в тот же вывод (задача явно требует отразить в `config show`).
- reviewer/services/summary_fragments.py:10 (`_GENERATION = "summary-fragment-v1"`) — bump в `"summary-fragment-v2"` инвалидирует все существующие fragment-провенансы через `has_complete_fragment_generation` (:67-90), не трогая БД вручную — критерий 5/6a.
- reviewer/index/summary_store.py:284-385 (`prune_verified_layout`) — транзакция, которая под advisory lock проверяет полное same-generation покрытие (`has_complete_fragment_generation`) и только тогда удаляет сироты + пишет `completed_layout`; это и есть штатный механизм критерия 7 (tests/* уходят без ручного SQL) при условии, что layout_token поменялся (критерий 8).
- plugin/skills/summarize-subsystems/SKILL.md:80-112 (шаги 5.1–5.3) — центральная точка правки: «tell the job to `Read` exactly that path» → `read_file(path, skeleton=True)` (шаг 1); «Dispatch exactly one file-summary job... for each pending entry» → батч-диспатч на порцию (шаг 3), сохранив правило отбраковки чужого `path` (:88-90) и запрет echo fingerprint (:87-88, PRI-291).
- plugin/skills/summarize-subsystems/SKILL.md:113-127 (шаг 6, prune) — уже описывает `prune_subsystem_summaries(repo, branch, layout_token, expected_source_hashes)` на полном проходе; логика не меняется, но текст шага 2/3 preflight (:56-68) должен явно упомянуть новый фильтр как компонент layout policy (сейчас там только depth/overrides).
(dropped 0 — весь найденный код по кластеризации/skeleton/generation-stamp/prune признан прямо информирующим)

## Test exemplars
- tests/skills/test_summarize_subsystems.py:108-116 (`test_skill_uses_incremental_file_summary_protocol`) — паттерн: грепает ассembled skill-текст на дословные фразы протокола; расширить аналогичной проверкой на `read_file(...skeleton=True)` вместо harness Read (критерий 1).
- tests/skills/test_summarize_subsystems.py:191-213 (`_file_job_step`, `test_skill_file_job_does_not_echo_fingerprint`, `test_skill_rejects_file_job_path_mismatch`) — паттерн: вырезает срез шага 5.2 между маркерами и проверяет инварианты батча/echo-fingerprint/mismatch; шаг 3 (батчинг) должен сохранить эти проверки работающими на новой формулировке "один субагент на порцию".
- tests/mcp/test_server.py:148-195 (`test_index_subsystem_summary_routes_typed_fragments_to_service`) — паттерн проверки, что FastMCP валидирует typed fragment payload и передаёт Pydantic-модели сервису; пригодится, если появится новый MCP-параметр/поле для фильтра или батч-размера.
(dropped 0)

## Constraints / open questions
- `search_codebase` дважды упирался в cliff-обрезку (15 из 29 и 15 из 65/59 результатов) — часть кода вокруг `list_subsystem_clusters`/`get_subsystem_summary_work` (счётчики added/changed/removed/moved, cap-логика) прочитана частично; перед реализацией стоит открыть `reviewer/mcp/service.py` целиком в районе 1794-2100 и `reviewer/entrypoints/mcp_server.py` 300-560.
- Не найдено явного места, где сегодня группируются файловые job'ы «один субагент на файл» помимо самого SKILL.md — размер порции (шаг 3) не имеет прецедента в коде, решение о величине батча и способе передачи нескольких путей в один промпт — открытый архитектурный вопрос для брейнсторминга.
- `get_task_context` не вернул связанных PR/задач для PRI-245 (только сама задача) — прямых артефактов реализации нет, вся опора на предшествующие завершённые задачи (PRI-165/154/280/291/283/287).
- Не найдено, где именно в `reviewer/entrypoints/cli.py` (`reviewer config show`) печатается `paths.ignore`/`summary_cluster_depth_overrides` построчно — только точка чтения эффективных значений в `reviewer/config/layers.py`; форматирование вывода `config show` нужно проверить отдельно перед правкой шага 2.
- Формула `layout_token` (критерий 8) сейчас — sha256 от `{default_depth, overrides}`; включение фильтра тестов должно математически стать частью этого payload (например, добавить `summary_paths_ignore` как третий отсортированный ключ) — конкретная схема payload не зафиксирована, решать в реализации.

Собран на: mid (claude-sonnet-5), сборка: subagent

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 44 · out 21.7K · cache-write 171.8K · cache-read 1.6M
Всего: 1.8M токенов
