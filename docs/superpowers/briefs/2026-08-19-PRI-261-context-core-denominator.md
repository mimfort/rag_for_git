# Brief — PRI-261 Знаменатель контекста для метрики брифа: контекстное ядро из графа рядом с core-recall

## Task
- Ключ: PRI-261 (стор: ID-315), доска yougile/PRI, статус «Запуск / CI / хуки», url: https://ru.yougile.com/team/686c049c8af8/#PRI-261
- Проблема: знаменатель `core-recall` — только файлы, изменённые настоящими PR-мержами задачи. Файл, который надо ПРОЧИТАТЬ, но не менять (контракт, соседний адаптер, образец), в знаменатель не входит → precision систематически занижена, а глубина фрагмента (`chars_per_file`) метрикой не видна вовсе.
- Что сделать: (1) спайк — вывести «контекстное ядро» из графа (CALLS/IMPLEMENTS, 1 vs 2 хопа) на 5–10 задачах и сверить глазами; (2) реализовать вывод в `reviewer/metrics/brief_quality/`; (3) добавить `context-recall` РЯДОМ с `core-recall`; (4) пересчитать precision по объединённому знаменателю, старую сохранить; (5) переснять 20×780 / 20×975 / 20×1300 на одном `indexed_sha`; (6) решить судьбу пола `chars_per_file ≥ 975`; (7) обновить CLAUDE.md.
- Критерии приёмки (7 шт., дословно в задаче): детерминированный вывод ядра из графа на историческом корпусе; спайк фиксирует размер ядра числом и может закрыть задачу отрицательным результатом; `context-recall` рядом, не вместо; пустой знаменатель → отдельный статус + NULL; замер на трёх точках глубины; онлайн-запись либо аддитивная идемпотентная миграция, либо не меняется (выбор назван явно); расчётное ядро остаётся одно, `eval/solve_task_metrics/` — ре-экспорт, guard-тест зелёный.
- Опорные числа (indexed_sha `308b86b`, N=44, bulk N=5): дефолт 20×1×975 → медиана core-recall 0.7500, bulk 0.5833; 20×780 → те же 0.7500/0.5833; 16×975 → 0.7639/0.4444. Онлайн «до»: `bulk_core_recall_median ≈ 0.373`, `bulk_n_measured = 4`.
- Не в скоупе: менять секции брифа кроме измерения; пере-гонять снятые рычаги (co-change, разворот кластеров subsystems).

## Related work
- PRI-259 (ID-313) — прямой родитель: раздел «Чего метрика не видит» в `eval/replay_report.md:473-495` формулирует ровно эту слепоту; там же прецедент «пол взят из здравого смысла, не из замера».
- PRI-249 (ID-303) — ввела онлайн-метрику `brief_quality` и правило «расчётное ядро одно на офлайн и онлайн»; её схема таблицы и статус `empty_core_denominator` — образец для нового статуса.
- PRI-257 (ID-311) — прецедент «нулевая дельта может быть механикой бюджета, а не сигналом»: перед вердиктом «ядро мусорное» проверять, доезжает ли сигнал.
- PRI-258 (ID-312) — прецедент отрицательного результата, закрытого числом (2 % точности); формат «рычаг снят, числа в отчёте».
- PRI-255/256 (ID-307) — источник рычагов, чью глубину метрика не различает.
- (dropped 3: ID-305/308/304 — про семейство символов и устройство харнесса, механизм другой.)

## Subsystems
- `reviewer/policy` — `ReviewPolicy` + `context_limits`: где живут `CodeSectionLimits` (`max_files`/`max_chunks_per_file`/`chars_per_file`), чьё влияние и надо научиться мерить.
- `reviewer/agent` — сборка/дедуп находок; смежно, не трогаем.
- (dropped 6: `tests/*` кластеры и `reviewer/tools` — не информируют реализацию напрямую.)

## Relevant code
- `reviewer/metrics/brief_quality/classify.py:7-23` — `is_core_production_path`: текущее определение ядра; рядом ляжет фильтр контекстного ядра. `categorize_miss` (:26-47) — образец таксономии.
- `reviewer/metrics/brief_quality/recall.py:32-88` — `TaskQuality`/`QualityAggregate`/`evaluate_task`/`aggregate`: сюда добавляются поля `context_*` РЯДОМ с существующими; `core_recall=None` при пустом ядре — образец для `context_recall=None`.
- `reviewer/metrics/brief_quality/briefs.py` — парсер брифов (`extract_section_paths`), вход `predicted`.
- `reviewer/graph/store.py:220` — `symbols_for_paths(repo, paths, branch)`: изменённые файлы → символы. `expand_detailed` (:171) и `expand` (:76) — обход CALLS/IMPLEMENTS на N хопов; `callers_detailed` (:96), `implementations_detailed` (:108), `bases_of` (:123) — точечные рёбра. Всё уже branch/repo-скоупно, `node_id = "path#fqn"` даёт обратный переход символ→путь.
- `reviewer/services/brief_quality.py:133-167` — онлайн-съём: `existed_before`, сборка `expected_core`, `STATUS_EMPTY_CORE`, поля строки (`expected_core_paths`, `hit_core_paths`). Точка решения по критерию 6 (миграция или офлайн-only).
- `reviewer/policy/context_limits.py:30-46` — `CodeSectionLimits`: дефолты `20/1/975`, свойство `max_chars`; менять ТОЛЬКО при различимой дельте.
- `reviewer/retrieval/multiquery.py:197-292` — **ключевая механика**: `diversify_by_file(max_files, max_chunks_per_file)` фиксирует НАБОР ПУТЕЙ, и лишь ПОТОМ `cap_block(item, sec.chars_per_file)` режет текст. См. «Constraints».
- `eval/solve_task_metrics/replay.py:84-102` — где строится `expected_core` в replay, куда добавляется контекстный знаменатель; `snapshot.py:84-100` — вторая точка с той же арифметикой.
- `eval/solve_task_metrics/ground_truth.py:28-45` — `filter_pr_merges`: ground truth только по настоящим PR-мержам (грабли синхронизационных мержей уже закрыты).
- `eval/solve_task_metrics/variants.py:44-80` — реестр вариантов replay (`_baseline`/`_limits`/`_multiquery`/`_similar_paths`) + `OVERRIDE_SECTIONS` с `code_section`: замер трёх точек глубины делается через `--set`, новый вариант не нужен.
- `eval/solve_task_metrics/{classify,recall,briefs}.py` — ре-экспорты; guard-тест на вторую копию.
- (dropped 8: `reviewer/{install,compose_lifecycle,web/api}.py`, `bugreport/triage.py`, `graph/{family,inherit,metrics}.py`, `tasks/*` — тот же ретрив вытащил, но реализации не касаются.)

## Test exemplars
- `tests/metrics/test_recall.py` — юниты `evaluate_task`/`aggregate` на чистых множествах; сюда же тесты `context_recall=None` при пустом контекстном знаменателе.
- `tests/services/test_brief_quality.py` — онлайн-съём с моками; образец проверки статуса и полей строки.
- `tests/eval/test_replay.py`, `tests/eval/test_snapshot.py` — прогон харнесса на фикстурах без git/БД.
- `tests/policy/test_context_limits.py` — дефолты и парс `code_section` из `.review.yml`.
- `tests/eval/test_history.py`, `tests/eval/test_forecast.py` — агрегация/тренд, если строка отчёта меняет форму.
- (dropped 8: `tests/skills/*`, `tests/web/*`, `tests/mcp/*`, `tests/retrieval/test_search_base.py` — вне фронта измерения.)

## Constraints / open questions
- **Критерий 5 предсказуем аналитически, и это надо сказать прямо в спайке.** В `multiquery.py:274-292` набор путей фиксирует `diversify_by_file` (только `max_files`/`max_chunks_per_file`), а `chars_per_file` применяется ПОСЛЕ, в `cap_block`, к тексту уже отобранных блоков. Заголовок блока `// node_id (path:start-end)` — первая строка, обрезка его не съедает, значит `extract_context_paths` вернёт ТОЖДЕСТВЕННОЕ множество путей на 20×780 / 20×975 / 20×1300 **по построению**. Любая метрика, считающая пути — старая или новая — к глубине слепа конструктивно; смена знаменателя эту слепоту не лечит. Замер трёх точек стоит прогнать как подтверждение, но решение по полу `chars_per_file` он не даст, и планировать «различимую дельту» от него нельзя. Если цель — реально измерить глубину, нужен другой класс метрики (по содержимому блока), и это отдельный разговор при брейншторме.
- Спайк-шаг (пункт 1) — настоящий гейт: если ядро на 1–2 хопах даёт десятки файлов на задачу, задача закрывается отрицательным результатом с числом. Порог «мусорности» в задаче не задан — назвать его ДО прогона.
- Прецедент PRI-257 (память + `.superpowers/sdd/2026-08-17-pri-257-augmented-candidates/step8-measurement.md`): нулевая/мусорная дельта могла быть механикой, а не сигналом — перед вердиктом проверять, что ядро вообще строится из тех символов.
- Граф на историческом корпусе: обход идёт на текущем `indexed_sha` (`308b86b`), а не на состоянии репо на момент каждой задачи — контекстное ядро исторической задачи выводится по СЕГОДНЯШНЕМУ графу. Это надо либо принять явно и записать как оговорку отчёта, либо ограничить корпус.
- Ветка/repo-скоуп графа: `:Symbol{repo, branch, id}` — все обходы обязаны передавать `branch` (реplay гоняется на `dev`).
- Критерий 6 требует явного выбора: аддитивная идемпотентная миграция `brief_quality` (`ADD COLUMN IF NOT EXISTS`) или офлайн-only. Дефолт-рекомендация — офлайн-only на первом шаге: онлайн-точка «до» (bulk 0.373, n=4) слишком мала, чтобы новая колонка успела набрать данные к приёмке.
- Гейт `REVIEW_HISTORY` общий; своего ключа у метрики нет.
- Преflight: `drift = 22` относительно `indexed_sha 308b86b` — для замера на «одном indexed_sha» это норма (сторона «до» снималась там же), но новый код в индекс не попадёт без `reviewer index`.
- Контекстные пробелы: нет (`gaps: []`); board-синк прошёл (118 задач, 0 изменений).

Собран на: Opus 5, сборка: inline
