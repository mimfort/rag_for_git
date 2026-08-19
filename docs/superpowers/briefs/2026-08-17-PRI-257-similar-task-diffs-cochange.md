# Brief — PRI-257 Подмешивание фактических diff-путей похожих задач и git-со-изменяемости
https://ru.yougile.com/team/686c049c8af8/#PRI-257

## Task
ID-311 / PRI-257. Секция `related.similar` уже находит похожие задачи, но их фактические
diff-пути (из истории прогонов) в ретрив секции `code` не подмешиваются. Добавить два новых
кандидатных сигнала: (1) пути реальных diff'ов похожих задач из `brief_quality`/`review_runs`,
(2) git-со-изменяемость файлов, исторически менявшихся вместе с уже найденными. Требования:
чисто server-side без LLM (в бриф — только пути), fail-soft (недоступная история/git → gaps,
не роняет сборку), доля подмешанных кандидатов ограничена и видна в отчёте, LLM-токены на
бриф не растут. Критерий приёмки: отдельно измеренная дельта bulk core-recall (replay-харнесс)
— не мержить при непокрывающей дельте.

## Related work
- PRI-307 (ID-307, subtask, родитель этой задачи) — «Повысить core-recall секции Relevant code
  брифа solve-task»: PRI-257 (ID-311) и PRI-312 (ID-312, разворот subsystems в файлы) — два
  параллельных дочерних рычага той же цели; следовать общей рамке измерения (replay + bulk
  core-recall) и не пересекаться по границам (тот рычаг трогает `subsystems`, не `code`).
- PRI-255 (ID-309, done) — ввёл `reviewer/retrieval/multiquery.py::search_multi`/`rrf_merge`:
  новый сигнал обязан войти в этот же пайплайн (кандидаты → `_dedupe_overlapping` →
  `diversify_by_file` → `cap_block`), а не быть параллельным путём рендера.
- PRI-256 (ID-310, done) — ввёл файловый бюджет `CodeSectionLimits` (`max_files`,
  `max_chunks_per_file`, `chars_per_file`) и `diversify_by_file`; критерий «доля подмешанных
  кандидатов ограничена» напрямую ложится на этот existing бюджет — нужна доля/квота внутри
  него, а не отдельный бюджет.
- PRI-254 (ID-308, done) — replay-харнесс (`eval/solve_task_metrics/`) и реестр вариантов
  `eval/solve_task_metrics/variants.py::_REGISTRY`; его докстринг прямо называет ID-311/312 как
  будущие one-line записи реестра — новый рычаг обязан завестись как вариант там, чтобы дельта
  bulk core-recall (критерий 1) мерилась тем же инструментом, что PRI-255/256.
- PRI-249/PRI-250 (done) — ввели саму метрику `brief_quality` (`reviewer/services/brief_quality.py`,
  `reviewer/web/history.py::record_brief_quality`) и линейку путей `reviewer/metrics/brief_quality/`
  — источник данных для сигнала №1 уже существует и не нужно создавать заново.
- PRI-235 (done) — `_repo_clone_path`/`repo_clone` таблица и паттерн «клон резолвится server-side,
  без похода в VCS API» — тот же паттерн годится для git-со-изменяемости (сигнал №2), не нужно
  тянуть VCS-провайдер.
- (dropped 0 из линии — ID-303/ID-164/ID-308 уже покрыты выше или являются инфраструктурой,
  напрямую не информирующей дизайн двух новых сигналов сверх сказанного)

## Subsystems
- reviewer/services — здесь же живёт `brief_quality.py` (источник сигнала №1) и понадобится новый
  git-helper рядом с `reviewer/gitutil.py` (источник сигнала №2).
- reviewer/policy — `CodeSectionLimits`/`ContextLimits.from_review_yaml` (`reviewer/policy/context_limits.py`)
  — сюда ложится новая доля/квота подмешанных кандидатов.
- reviewer/agent — общий паттерн сборки/дедупа контекста (`_dedupe_overlapping`,
  `diversify_by_file` в multiquery.py логически рядом), полезен как референс для дедупа
  кандидатов из разных источников.

## Relevant code
- reviewer/retrieval/multiquery.py#search_multi (multiquery.py:145-185) — точка интеграции:
  после `rrf_merge` и `_graph_items` (строки 175-178), перед `_dedupe_overlapping`/`diversify_by_file`
  (строки 181-183), нужно добавить third source — items, собранные из путей похожих задач и
  co-change, через `retriever.store.fetch_nodes(repo, paths_or_ids, "__none__", [], base_ref=bref)`
  (тот же вызов, что `_graph_items` строка 113-114) — паттерн «путь → чанк из стора» уже есть,
  копировать его, а не изобретать. Blast radius: 15 caller-тестов в `tests/retrieval/test_multiquery.py`
  (см. callers) плюс `MCPReviewService._search_codebase_multi` (service.py:1797) — единственный
  продакшн-вызывающий; любая правка сигнатуры `search_multi` требует правки обоих мест и live.py:130-145.
- reviewer/retrieval/multiquery.py#diversify_by_file (multiquery.py:122-142) — оставляет
  ≤max_chunks_per_file на файл и ≤max_files файлов, идёт по входному порядку (приоритет источника
  = порядок в списке `items`); если новые кандидаты добавлять ПОСЛЕ hybrid+graph (как в строке 177),
  они естественно получат низший приоритет при вытеснении — вероятно то, что нужно для критерия 4
  («не вытесняют выдачу гибрида полностью»), но нужна явная квота, а не только позиция в списке
  (см. Constraints).
- reviewer/policy/context_limits.py#CodeSectionLimits (context_limits.py:30-56) и
  `ContextLimits.from_review_yaml` (context_limits.py:66-99) — новая доля кандидатов должна стать
  полем здесь (например `similar_task_paths_ratio`/`cochange_ratio` или единый
  `max_augmented_candidates`), читаемым из `.review.yml` тем же путём, что остальные лимиты
  (env-слоя нет у этого блока — см. докстринг PRI-245-аналог).
- reviewer/services/brief_quality.py#BriefQualityMeasurement (brief_quality.py:34-51) и
  `measure` (brief_quality.py:87-168) — `expected_core_paths` (строка 50, наполняется в строке 166:
  `predicted & expected_core` — ВНИМАНИЕ, это только пересечение с предсказанным, НЕ все
  expected_core; полное множество ожидаемых core-путей — в переменной `expected_core` внутри
  `measure`, но в датаклассе как множество путей не сохраняется отдельным полем — только
  `expected_core: int` счётчик). Нужен новый источник для «фактических diff-путей похожей задачи»:
  либо разобрать `predicted_paths ∪ hit_core_paths` (оба — множества путей уже в БД), либо
  завести новую колонку `expected_paths`/`expected_core_paths_full`. См. Constraints — какая
  колонка реально несёт «все фактически изменённые core-пути задачи», а не пересечение с
  предсказанием прошлого прогона, требует решения до кода.
- reviewer/web/history.py#History.record_brief_quality (history.py:526-581) и schema
  `brief_quality` (reviewer/web/schema.sql:99-126, колонки `task_key TEXT`, индекс
  `brief_quality_task_key ON brief_quality (task_key)` строка 126) — точка чтения для сигнала №1:
  нет ещё метода «пути по списку task_key», понадобится новый read-метод (например
  `paths_for_task_keys(keys: list[str]) -> dict[str, list[str]]`) рядом с `brief_quality_trend`
  (history.py:583+), который уже группирует по `task_key` тем же паттерном union-по-задаче
  (history.py:586-593) — переиспользовать эту логику агрегации, а не писать новую.
- reviewer/mcp/task_context.py#build_task_context (task_context.py:69-117) — `deps.code(repo,
  branch, _queries(task, key))` (строка 111) на входе не имеет доступа к ключам похожих задач:
  `payload["related"]["similar"]` (строки 104-106) — уже ОТРЕНДЕРЕННАЯ строка (см.
  `_TaskContextDeps.similar`, service.py:3551-3552, зовёт `search_tasks` → `TaskService.search_tasks`
  service.py:501-504/tasks/service.py:318-345, которая форматирует хиты в текст СРАЗУ). Ключи
  похожих задач нужно достать СТРУКТУРНО (`hit.key`), а не парсингом текста — иначе хрупкий
  regex по человекочитаемому формату. Нужен новый структурный путь: либо `TaskService` получает
  метод, возвращающий сырые хиты (`self._store.search(...)`, tasks/service.py:329, уже даёт
  объекты с `.key`), либо `code()` в `_TaskContextDeps` (service.py:3557-3558) сам зовёт этот
  структурный поиск параллельно с рендером `similar`, без изменения формата `related.similar`.
- reviewer/mcp/service.py#_TaskContextDeps.code (service.py:3557-3558) и
  `MCPReviewService._search_codebase_multi` (service.py:1797-1819) — здесь физически добавляется
  новый провайдер: либо `code()` передаёт в `search_multi` доп. параметр
  `augment_paths: list[str]`, либо появляется отдельная функция в multiquery.py, вызываемая
  отсюда. `_repo_clone_path` (service.py:1640-1656) — существующий паттерн server-side резолва
  клона (PRI-235) для git-со-изменяемости: git-хелпер должен принимать этот путь, fail-soft как
  `CommittedLayerFetcher`.
- reviewer/gitutil.py (весь модуль, функции строк 7-90) — нет функции co-change/git-history;
  ближайший аналог `changed_files(repo, base, head)` (строка 36) делает `git diff --name-only`
  между двумя ref, а не обход истории коммитов одного пути. Новую функцию (например
  `cochanged_paths(repo_path, paths, limit_commits)` — `git log --name-only -n N -- <path>` на
  каждый seed-путь, подсчёт частоты со-появления) придётся писать с нуля по образцу `_git`
  (строка 7) и `changed_files` (обработка вывода git построчно).
- eval/solve_task_metrics/variants.py#_multiquery (variants.py:60-68) и `_REGISTRY`
  (variants.py:71-75) — образец для нового варианта (например `_similar_diffs_cochange`),
  докстринг модуля (строки 1-6) прямо предписывает добавить рычаг ID-311 одной строкой реестра.
- eval/solve_task_metrics/live.py#LiveRetrieval.code_multi (live.py:126-145) — живой провайдер,
  который зовёт продакшн-путь `_search_codebase_multi`/`search_multi` дословно; если сигнатура
  `code()`/`search_multi` меняется (доп. параметр augment), этот метод и variants.py оба требуют
  синхронной правки — тот же класс риска, что описан в докстринге live.py (строки 7-9): «своей
  копии пути ретрива не заводить».

## Test exemplars
- tests/retrieval/test_multiquery.py#test_file_budget_caps_distinct_files (test_multiquery.py:186)
  и test_dedupe_runs_before_diversify_keeps_enclosing_class (test_multiquery.py:262) — паттерн
  проверки порядка «dedupe → diversify» и файлового бюджета; новый источник кандидатов обязан
  проходить через тот же пайплайн, эти тесты — контракт, который нельзя сломать.
- tests/retrieval/test_multiquery.py#test_graph_only_hit_appended_after_hybrid_without_duplicating
  (test_multiquery.py:203) и test_graph_only_tail_yields_to_hybrid_files (test_multiquery.py:282) —
  прямой прецедент «третий источник кандидатов не должен вытеснять hybrid» — писать новые тесты
  для similar-task/co-change источника по этому же шаблону (приоритет позиции в списке + квота).
- tests/mcp/test_prepare_task_context.py#test_every_failure_still_returns_all_sections
  (test_prepare_task_context.py:161) и test_postgres_down_empties_retrieval_sections
  (test_prepare_task_context.py:102) — fail-soft контракт `build_task_context`: новый источник,
  недоступный (история/git), обязан дать `gaps`-запись, а не exception — использовать `_safe`
  (task_context.py:31-38) как есть, не изобретать новый механизм.
- tests/mcp/test_prepare_task_context.py#test_code_section_receives_subquery_list
  (test_prepare_task_context.py:208) — образец теста «что именно долетает до `deps.code`»;
  аналогичный тест понадобится для проверки, что похожие task-ключи/co-change пути долетают до
  `search_multi`, а не теряются в `_TaskContextDeps.code`.
- tests/eval/test_replay_report.py#test_ab_report_shows_per_task_delta_with_path_diff
  (test_replay_report.py:70-76) — инструмент замера дельты по задаче (критерий 1); новый вариант
  реестра должен прогоняться этим же A/B отчётом до/после для bulk core-recall.
- (структура тестов брифа/git-со-изменяемости пока не существует — новые модули потребуют новых
  test-файлов `tests/services/test_...cochange...py` и `tests/services/test_brief_quality_paths...py`
  по аналогии с `tests/services/test_brief_quality.py`, если он есть — не подтверждено чтением)

## Constraints / open questions
- **Какая колонка `brief_quality` реально хранит «фактические diff-пути похожей задачи»?**
  `expected_core_paths` в БД — это МНОЖЕСТВО core-путей, ожидаемых для задачи (полный diff ∩
  core-классификация), не пересечение с предсказанием — см. `measure()` строка 140-144
  (`expected_core = {...}`) и строка 166 (`expected_core_paths=tuple(sorted(expected_core))`).
  Это и есть искомое «фактические diff-пути», уже готовое, БЕЗ новой колонки — нужно
  перепроверить это чтением `measure()` ещё раз перед кодом (я прочитал верно один раз, но
  критично для дизайна — стоит перечитать `tests/services/test_brief_quality.py`, если есть).
  `predicted_paths`/`hit_core_paths` — НЕ то, что нужно (это предсказание прошлого прогона).
- Похожая задача может иметь 0..N строк `brief_quality` (несколько PR/прогонов) и не иметь ни
  одной, если у неё никогда не публиковался ревью с посчитанной метрикой (`status='measured'`
  только). Нужно решить: берём последнюю по `created_at`, объединяем все (как `brief_quality_trend`
  делает union по задаче, history.py:586-593), или top-1?
- Нет готового способа достать СТРУКТУРНЫЕ ключи похожих задач (`hit.key`) в
  `prepare_task_context`: `related.similar` уже отрендерен в текст до попадания в payload.
  Нужно решить архитектуру: (а) `_TaskContextDeps.code()` сам вызывает низкоуровневый
  `task_service._store.search(...)` второй раз (дублирование запроса/эмбеддинга — доп. Voyage-
  вызов, что противоречит «не увеличивать LLM-токены», хотя эмбеддинг — не LLM-токен и это
  Voyage 3 RPM/10K TPM лимит, отдельная забота); или (б) `build_task_context` передаёт уже
  вычисленные ключи `similar` в `code()` — требует правки протокола deps (сейчас `code(repo,
  branch, queries)`, нужно `code(repo, branch, queries, similar_keys)`) и синхронной правки
  `TaskService.search_tasks`, чтобы отдать хиты, не только текст, без второго похода в стор.
- Git co-change не имеет готовой реализации нигде в репо — метод расчёта (простое совпадение
  в одном коммите vs. окно из N последних коммитов vs. `git log --follow -p` по каждому seed-
  пути) не выбран; нужно решить порог частоты (co-change ≥ K раз) и глубину истории (`-n N`),
  иначе на большом репо `git log --name-only` без лимита — дорогая по времени операция на
  каждый `prepare_task_context`.
- `.review.yml`-параметризация новой доли/квоты: куда класть — новое поле в `CodeSectionLimits`
  (PRI-256) или отдельный `TasksLimits`-подобный блок? `CodeSectionLimits.max_files` уже
  единственный источник «сколько файлов в секции» — если новые кандидаты идут в общий пул ДО
  `diversify_by_file`, отдельная квота не нужна (позиция в списке уже даёт приоритет hybrid),
  но тогда «доля подмешанных кандидатов ограничена и видна в отчёте» (критерий 4) требует
  считать её ПОСТ-ФАКТУМ (сколько из итоговых max_files оказалось из augmented-источника) и
  положить в `ContextPack`/лог, а не ограничивать заранее — нужно решить, что именно значит
  «видна в отчёте»: лог warning, поле в payload, или debug-метрика Prometheus-стиля (в проекте
  таких нет — ближайший аналог видимости — `gaps`/`warnings` в payload).
- Критерий 3 «недоступность review_runs/brief_quality ИЛИ git не прерывает сборку» — покрывается
  паттерном `_safe`/try-except как везде (fail-soft — норма проекта), но конкретно где ловить:
  внутри `search_multi` (как `_graph_items`, multiquery.py:104-119, try/except с `log.warning`)
  или на уровне `_TaskContextDeps.code()` — решить по аналогии с существующими источниками
  (ближе к `_graph_items`, т.к. это тоже «третий источник кандидатов внутри одного ретрива»).
- Не проверено чтением: есть ли отдельный gate `REVIEW_HISTORY`/аналог для этой новой фичи —
  вероятно да (без включённой истории `brief_quality` данных вообще нет — сигнал №1 сам себя
  выключает при `REVIEW_HISTORY=false`), но это не подтверждено, нужно свериться с
  `Settings.review_history` при реализации.
- eval-вариант (критерий 1) требует, чтобы `LiveRetrieval` (live.py) тоже умел вызывать новый
  augmented-путь — а `LiveRetrieval` не имеет доступа к `history`/`brief_quality` таблице
  напрямую (только к `components`/`service`) — нужно прокинуть `self._service._review_service
  ._ensure_history()` или аналог, паттерн есть в service.py:3236 (`_record_brief_quality`), но
  не проверен на доступность из `LiveRetrieval`.

Собран на: mid (Sonnet), сборка: subagent
