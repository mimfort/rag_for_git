# Brief — PRI-259 Свод: отбор рычагов по замеру и локализация правок в сборке брифа
https://ru.yougile.com/team/686c049c8af8/#PRI-259

## Task
ID-313 / PRI-259, статус «Движок (reviewer CLI/MCP)». Четыре рычага (мультизапрос PRI-255,
файловый бюджет PRI-256, подмешивание similar-diffs PRI-257, разворот кластеров subsystems
PRI-258) реализованы независимо — нужен сводный замер их комбинаций, отбраковка непокрывающих
и локализация правок в сборке брифа (`_TaskContextDeps.code`, `reviewer/mcp/task_context.py`)
без изменения общего `Retriever.search_base`. Критерии: (1) bulk core-recall (ядро ≥10 файлов)
≥0.55 без падения общей медианы; (2) отчёт с дельтой каждого рычага и их суммы, каждый смерженный
рычаг — с положительной дельтой; (3) `search_base` не тронут либо изменения за solve-task-флагом,
покрыто тестом; (4) `brief_quality` пишется и сопоставимо с точкой «до»; (5) расход токенов
сборки брифа не вырос.

## Related work
- PRI-255 — мультизапрос + RRF (`reviewer/retrieval/multiquery.py`), уже смержен: переиспользовать
  как базовую сторону «до» для сводного замера, не переделывать.
- PRI-256 — файловый бюджет секции code (`CodeSectionLimits`), уже смержен: переиспользовать
  `--set code_section.*` в `eval replay` для комбинаций лимитов, если понадобится.
- PRI-257 — подмешивание similar-diffs, уже смержен, единственный рычаг с измеренной
  положительной дельтой (+0.25 медианы, bulk 0.3944); co-change реализован и снят — не
  воспроизводить. Даёт готовый шаблон отчёта приёмки и три задокументированные ловушки
  «нулевая дельта из-за механики бюджета, а не сигнала» — обязательно проверять по этому
  шаблону любой новый/комбинируемый рычаг.
- PRI-258 — разворот кластеров subsystems в пути, реализован и снят (2% точность, вытеснение
  core-хитов, медиана −0.0833): не включать в комбинации по умолчанию, только если для задачи
  найдётся новый вход, отличный от уже проверенного.
- PRI-253 (родитель) — критерии приёмки родителя цитируются в задаче как контекст, но текста
  самой PRI-253 в контексте нет; предполагать без риска нельзя (пометить как open question).
(dropped 4: ID-308/ID-138/ID-178 — методология харнесса и общего search_codebase, не рычаги
секции code задачи; ID-312 — то же событие, что PRI-258, уже учтено выше)

## Subsystems
- reviewer/retrieval — Retriever.search_base, search_multi, augment.py: место всех четырёх рычагов
  и их конфигурации.
- reviewer/mcp — service.py::_TaskContextDeps, task_context.py: точка локализации правок по
  критерию 3, единственный производитель queries в секцию code.
- reviewer/policy — context_limits.py::CodeSectionLimits: единственная точка настройки бюджета
  секции, кандидат для solve-task-специфичного флага/лимита.
- reviewer/metrics/brief_quality — recall.py, briefs.py, classify.py: онлайн-метрика, критерий 4.
- reviewer/services — brief_quality.py: запись метрики из publish_review, union путей по задаче.
- eval/solve_task_metrics — variants.py, replay.py, replay_report.py: харнесс сводного замера,
  единственное место, где регистрируются варианты и куда добавлять комбинации.
- tests/retrieval, tests/mcp — покрытие search_base/multiquery/augment/prepare_task_context.
(dropped 0)

## Relevant code
- `eval/solve_task_metrics/variants.py:87-104` — `_REGISTRY` из 4 вариантов (`baseline`,
  `limits`, `multiquery`, `similar_paths`); `cochange`/`subsystem_paths` в реестре уже нет —
  сводный замер комбинаций должен либо временно вернуть их код для замера, либо гонять через
  `--set` оверрайды без регистрации нового варианта. Новая комбинация регистрируется одной
  строкой (комментарий в докстринге модуля это явно обещает).
- `eval/solve_task_metrics/replay.py:111-198` — `run_replay`: строит `TaskInput` по корпусу,
  вызывает `variants.get_variant(name)`, считает `evaluate_task`/`aggregate`, пишет снапшот с
  `variant`/`variant_params`. Здесь же логика записи в `eval/replay_history.jsonl`.
- `eval/solve_task_metrics/replay_report.py:39-41` — рендер `eval/replay_report.md`; **весь файл
  перезаписывается при каждом прогоне** — разделы приёмки прошлых PRI восстанавливаются вручную
  (задокументированная процедура в самом отчёте).
- `reviewer/metrics/brief_quality/recall.py:13,36-47,50-64,67-88` — `BULK_CORE_THRESHOLD = 10`,
  `QualityAggregate` (`bulk_core_recall_median`, `bulk_n_measured`, `core_recall_median`),
  `evaluate_task`, `aggregate`. Это единственное место, где считаются числа критериев 1 и 4;
  `eval/solve_task_metrics/{classify,recall,briefs}.py` — ре-экспорт этого модуля (см.
  guard-тест ловящий вторую копию, CLAUDE.md).
- `reviewer/services/brief_quality.py:87-167` — `measure()`: `existed_before` фильтр по
  `changed_status`, `expected_core` через классификацию core-путей, запись `expected_core_paths`/
  `hit_core_paths`; вызывается только при `posted and not dry_run` из `publish_review` —
  критерий 4 сравнивается с этой же функцией, менять её форму нельзя без риска сломать
  сопоставимость с точкой «до» (`bulk_core_recall_median ≈ 0.373, bulk_n_measured = 4`).
- `reviewer/mcp/service.py:3610-3618` — `_TaskContextDeps.code()`: точка сборки `AugmentSource`
  списка и вызова `_search_codebase_multi`; это и есть локализация критерия 3 (номера строк из
  тикета 3532-3536 устарели — актуальный диапазон 3610-3618, `_TaskContextDeps` класс начинается
  на 3513).
- `reviewer/mcp/service.py:1797-1824` — `_search_codebase_multi`: приватный путь, явно
  задокументирован как параллельный `search_base` («публичный search_codebase остаётся
  однозапросным, чтобы /ask, грунтовка и ревью PR не меняли поведение») — это готовый прецедент
  локализации, которому новый рычаг должен следовать; не трогать `search_codebase` (публичный).
- `reviewer/retrieval/retriever.py:155-226` — `Retriever.search_base` (единственное
  определение) — критерий 3 требует его неизменности или флага; ни один из 4 рычагов пока его
  не менял (все живут в `multiquery.py`/`augment.py`, которые явно объявлены «параллельными,
  не меняющими search_base»).
- `reviewer/retrieval/multiquery.py:1-11,220` — модульный докстринг прямо фиксирует инвариант
  «путь параллелен Retriever.search_base и не меняет его»; `search_multi()` — точка входа
  файлового бюджета/подмешивания/диверсификации.
- `reviewer/retrieval/augment.py:1-16,56` — модульный докстринг фиксирует историю снятых рычагов
  (co-change, subsystem-cluster) и списочную архитектуру `AugmentSource`; `collect_similar_task_paths`
  — единственный продакшн-источник подмешивания сейчас.
- `reviewer/policy/context_limits.py:29-58` — `CodeSectionLimits`: `max_files=12`,
  `max_chunks_per_file=1`, `chars_per_file=1300`, `max_augmented_files=3`; кандидат-точка для
  solve-task-специфичного лимита/флага, если новая комбинация рычагов потребует своей настройки
  бюджета, не трогая CodebaseLimits (обслуживает /ask и ревью PR).
- `plugin/hooks/brief_cost.py:1-10,124-156` — хук `PostToolUse`, включается флагом
  `.review.yml`: `solve_task.brief_token_cost` (сейчас `true` в корневом `.review.yml:26`);
  измеряет расход токенов сборки брифа детерминированно по транскрипту — источник числа для
  критерия 5 «не вырос».
(dropped 3: reviewer/web/api.py и history.py::record_run — наблюдаемость прогонов PR-ревью,
не относится к сборке брифа задачи; reviewer/index/store.py::hybrid_search SQL-копия RRF_K —
известный несвязанный долг, не в скоупе локализации)

## Test exemplars
- `tests/retrieval/test_search_base.py:119-296` (весь файл, ~30 тестов на `search_base`) —
  существующее покрытие поведения `search_base`; ни один тест явно не утверждает «этот путь
  не меняется multiquery/augment» — при добавлении флага/комбинации рычагов сюда стоит добавить
  regression-тест «вызов `_search_codebase_multi` не проходит через `search_base`» (сейчас такого
  теста нет — пробел под критерий 3).
- `tests/retrieval/test_multiquery.py:317-357,507-530,627` — `test_two_sources_get_separate_quotas_and_named_note`,
  `test_augment_quota_counts_files_with_chunks_not_candidates_considered`,
  `test_augmented_candidates_ordered_by_raw_pool_rank` — паттерн проверки квоты/приоритета
  источника подмешивания через фейковый `retriever`/`store`; шаблон для теста новой комбинации
  рычагов на уровне `search_multi`.
- `tests/retrieval/test_augment.py:10-92` (весь файл, 7 тестов) — паттерн: `AugmentResult`
  иммутабелен, гэпы при сбое истории/git, лимит путей; мокается `gitutil.paths_touched_by_grep`
  и объект истории с `Mock`/фейком без реальной БД.
- `tests/mcp/test_prepare_task_context.py:5-50,208-234,271-288` — `FakeDeps` (фейк-провайдер
  секций) и `test_code_section_receives_subquery_list`,
  `test_augment_gaps_are_copied_into_payload`,
  `test_similar_section_text_is_unchanged_by_augmentation` — паттерн юнит-теста сборки без
  Postgres/Neo4j/сети (весь модуль `task_context.py` тестируется через FakeDeps).
(dropped 1: tests/services/test_brief_quality.py — не найден отдельным поиском в этой сессии,
нужно грепнуть отдельно перед реализацией критерия 4, чтобы не задваивать покрытие метрики)

## Constraints / open questions
- Критерий 1 (bulk core-recall ≥0.55) при текущем продакшн bulk 0.3889 (`similar_paths`,
  приёмка PRI-258) — **главная развилка брейншторма**: неизвестно, достижим ли порог одним
  отбором/комбинацией уже смерженных рычагов (PRI-255+256+257) или комбинация даёт тот же
  потолок и нужен новый, ещё не изобретённый рычаг. Все 4 существующих рычага уже либо смержены
  поодиночке (255/256/257), либо однозначно сняты (258/co-change) — «отбор» частично сводится
  к ретро-фиксации уже принятых решений плюс добору неисследованных комбинаций (единственная
  измеренная комбинация «оба» — similar-diffs+co-change, дала тот же результат, что один
  similar-diffs; similar-diffs+subsystem_paths измерена и хуже одного similar-diffs).
- bulk-подвыборка мала: PRI-257 измерял на 42 задачах корпуса, но `bulk_n_measured` (реальная
  онлайн-метрика с порогом ≥10 файлов) — всего 4 в проде; офлайн bulk-выборка replay тоже
  на уровне единиц-десятков задач с ядром ≥10 файлов. Медиана по такому N — шумная величина,
  ±1 задача может двигать её на 0.1-0.2.
- Индекс dev только что переиндексирован (951e791 → 308b86b, drift был 36). Все прошлые числа
  приёмки (PRI-255/256/257/258) сняты на `indexed_sha=951e791`. Сравнимость точки «до»/«после»
  для PRI-259 под вопросом — либо переснимать baseline на новом sha, либо явно фиксировать sha
  в отчёте и не сравнивать дельты между разными индексами напрямую (`eval/replay_report.md`
  прямо требует одного `indexed_sha` на прогон).
- Критерий 3 (search_base не тронут / флаг) — сейчас де-факто выполняется архитектурно
  (`multiquery.py`/`augment.py` — параллельный путь), но **нет теста, который бы ловил
  регрессию**, если кто-то решит менять `search_base` напрямую вместо параллельного пути; это
  нужно закрыть новым тестом, а не констатацией «и так не трогали».
- PRI-253 (родитель) прочитана из стора: её критерии 1-7 — воспроизводимость baseline
  (медиана 0.67, bulk 0.373, bulk_n_measured=4), дельта каждого из ЧЕТЫРЁХ рычагов отдельно
  и в сумме (рычаг с непокрывающей дельтой не мержится), bulk ≥0.55 без падения общей медианы,
  измеримая зависимость числа подзапросов от размера задачи, неизменность расхода LLM-токенов
  (рост Voyage-эмбеддингов в приёмку НЕ входит), сохранность онлайн-метрики brief_quality и
  неприкосновенность `Retriever.search_base` для ask/ревью PR. Критерии PRI-259 — их подмножество,
  противоречий нет; PRI-259 закрывает родителя.
- Задача явно про **отбор и локализацию**, не про новую реализацию: бриф не должен предлагать
  решение — только карту существующих рычагов, харнесса и точек локализации для брейншторма.

Собран на: mid (Sonnet), сборка: subagent
