# Brief — PRI-254 Replay-режим офлайн-харнесса: прогон ретрива по корпусу против ground truth

https://ru.yougile.com/team/686c049c8af8/#PRI-254

## Task
`eval/solve_task_metrics/` меряет ретроспективно: `briefs.load_briefs` парсит **уже написанные**
брифы из `docs/superpowers/briefs/` и сопоставляет пути их секции `## Relevant code` с ground truth
(PR-мержи по ключу задачи, `ground_truth.collect`). Прогнать по тому же корпусу **новый** алгоритм
ретрива невозможно — брифы фиксированы, поэтому дельта каждого рычага PRI-253 (подзадачи ID-307,
ID-311, ID-312, ID-313) непроверяема и рычаги пришлось бы мержить на веру.

Нужен режим `replay` рядом с существующими `snapshot|stats|compare|forecast`:
1. Для каждой задачи корпуса заново собрать кандидатов секции `code` **вызовом целевой функции
   сборки брифа** и сопоставить с ground truth той же линейкой (`reviewer/metrics/brief_quality/`).
2. Вход задачи для replay — **текст задачи из стора**, а не текст брифа: replay должен видеть ровно
   то, что видит `prepare_task_context` в проде.
3. A/B: прогон с вариантом конфигурации ретрива + сравнение агрегатов с baseline в одном отчёте
   (дельта core-recall, bulk core-recall, precision, число предсказанных файлов).
4. Существующие подкоманды не менять.

Критерии приёмки из тикета (см. блокирующее расхождение в Constraints):
1. Replay без изменений в ретриве воспроизводит опубликованный baseline: медиана core-recall 0.67,
   bulk 0.373, `bulk_n_measured = 4`.
2. Отчёт A/B показывает дельту по задачам **и** агрегату, а не только итоговое число.
3. Расчётное ядро берётся из `reviewer/metrics/brief_quality/`, вторая копия не заводится
   (guard-тест PRI-249 остаётся зелёным).
4. Прогон воспроизводим: один и тот же коммит и корпус дают тот же агрегат.

## Related work
- **ID-304 (done, PR #198)** — сам харнесс: `snapshot/stats/compare/forecast`, история срезов,
  ground truth по PR-мержам. Здесь только replay поверх готовой инфраструктуры.
- **PRI-249 (done)** — перенос расчётного ядра в `reviewer/metrics/brief_quality/` + онлайн-съём
  `brief_quality` при `publish_review`; guard-тест направления зависимости.
- **PRI-253 / ID-313** — свод: отбор рычагов по замеру. Replay — измерительный прибор для него.
- **ID-307** (повысить core-recall секции `## Relevant code`), **ID-311** (подмешивание diff-путей
  похожих задач + git-со-изменяемость), **ID-312** (разворот кластеров subsystems в файлы) —
  потребители A/B-режима; их варианты конфигурации должны выражаться как «вариант ретрива» replay.
(dropped 0)

## Subsystems
- `eval/solve_task_metrics` — офлайн-харнесс (не в индексе: `eval` в `paths.ignore`). Сюда садится
  `replay`. Модули `briefs/classify/recall` здесь — **ре-экспорты**, не копии.
- `reviewer/metrics/brief_quality` — единственная копия формул (`recall.evaluate_task`,
  `recall.aggregate`, `classify.is_core_production_path`, `briefs.extract_section_paths`).
- `reviewer/mcp` — `task_context.build_task_context` + `_TaskContextDeps`: и есть «целевая функция
  сборки брифа», её `code`-секция — предмет замера.
- `reviewer/retrieval` + `reviewer/policy` — `Retriever.search_base` и `CodebaseLimits`: поверхность,
  которую варьирует A/B.

## Relevant code
- `eval/solve_task_metrics/__main__.py:203` — `main()`/subparsers: точка добавления `replay` рядом с
  `snapshot|stats|compare|forecast` (менять их нельзя).
- `eval/solve_task_metrics/snapshot.py:20` — `build_snapshot`: канонический цикл «корпус → дедуп по
  ключу → ground truth → `recall.evaluate_task` → `recall.aggregate`». Replay переиспользует всё,
  кроме источника `predicted`.
- `eval/solve_task_metrics/snapshot.py:84` — построение `expected_core` + кэш `existed()`: линейка,
  которую replay обязан взять как есть.
- `eval/solve_task_metrics/ground_truth.py:118` — `collect`: PR-мержи задачи и объединение diff'ов;
  `path_existed` (`:91`), `PR_MERGE_SUBJECT_RE` (`:27`).
- `eval/solve_task_metrics/report.py:19` — `render`: образец markdown-отчёта со сводкой + per-task
  таблицей; A/B-отчёт строится по этому же образцу.
- `eval/solve_task_metrics/history.py` — `append_snapshot`/`load_snapshots`/`diff_snapshots`/`SCHEMA`:
  готовый механизм дельт агрегатов; A/B может лечь на него или на отдельный файл истории replay.
- `eval/solve_task_metrics/briefs.py:1`, `recall.py:1`, `classify.py:1` — ре-экспорты; расширять их,
  а не дублировать формулы.
- `reviewer/mcp/task_context.py:58` — `build_task_context`: секции payload, fail-open через `_safe`.
- `reviewer/mcp/task_context.py:39` — `_query`/`_test_query`: **как из задачи строится запрос
  ретрива** (`title` + первые 8 строк `description`) — это и есть «вход из стора».
- `reviewer/mcp/service.py:3480` — `_TaskContextDeps`: `code()` (`:3533`) = `search_codebase(...)`,
  `subsystems()` (`:3530`), `test_exemplars()` (`:3536`). Точка подмены для replay без сети к доске
  (`warm_board` не нужен).
- `reviewer/mcp/service.py:1797` — `prepare_task_context`: резолв repo/branch, лимиты.
- `reviewer/mcp/service.py:1770-1795` — `search_codebase`: `_resolve_context_limits` →
  `retriever.search_base` → `pack.as_context(line_numbers=True)`. Формат вывода — `// path#fqn
  (path:start-end)` + нумерованные строки; из него replay извлекает пути-кандидаты.
- `reviewer/retrieval/retriever.py:152` — `search_base`: ANN-префильтр → graph-expansion → rerank →
  cliff. Всё, что варьирует A/B, проходит здесь.
- `reviewer/policy/context_limits.py:8` — `CodebaseLimits` (floor/ceiling/ratio/abs_floor/
  candidate_pool/ann_distance_max) + `from_review_yaml`: естественный формат «варианта конфигурации».
- `reviewer/metrics/brief_quality/briefs.py:130` — `extract_section_paths`: парсер путей секции
  брифа. Для replay нужен **другой** извлекатель (вывод `as_context`, а не bullet-список) —
  вопрос, где он живёт (см. Constraints).
- `reviewer/services/brief_quality.py:87` — `measure`: онлайн-съём той же метрики; эталон того, как
  «отказ = именованный status», а не молчание.
(dropped 0)

## Test exemplars
- `tests/eval/test_snapshot.py` — юнит-тесты `build_snapshot` на инъектируемом `run_git` (без git);
  образец для тестов replay на инъектируемом источнике ретрива.
- `tests/eval/test_ground_truth.py`, `tests/eval/test_history.py`, `tests/eval/test_forecast.py` —
  чистые тесты офлайн-модулей.
- `tests/eval/test_docs.py:16` — guard: **обе** версии README документируют каждую подкоманду
  (`snapshot|stats|compare|forecast`). Добавление `replay` обязано обновить и тест, и оба README.
- `tests/metrics/test_reexport_guard.py:17` — guard PRI-249 (`is`-идентичность объектов) и `:29`
  (`reviewer/**` не импортирует `eval/**`). Критерий 3 — ровно этот файл.
- `tests/mcp/test_prepare_task_context.py:1` — фейковый `deps`, форма payload и посекционный
  fail-open; готовая поверхность подмены для replay-тестов.
- `tests/services/test_brief_quality.py:201` — `test_online_matches_offline_formula_on_full_diff`:
  образец теста-стыка «две линейки считают один вход одинаково».
(dropped 0)

## Constraints / open questions

**Блокирующее расхождение в критерии 1 — числа 0.67 не существует ни в одном артефакте.**
Проверено на репозитории:
- `eval/solve_task_metrics_history.jsonl` (8 срезов, последний `d474e02`): `core_recall_median`
  **0.5556**, `n_measured` 35, `bulk_core_recall_median` **0.373**, `bulk_n_measured` **4**.
- `eval/solve_task_metrics_report.md`: «core-recall: медиана 56%, среднее 60%, N=35».
- Спайк `eval/pri246_report.md`: «filtered recall — **медиана 61%**, N=34».
- `CLAUDE.md` (раздел про PRI-249) утверждает «медиана 15 % против **67 %** у core (спайк PRI-246)» —
  и это, судя по всему, источник числа в тикете; самому спайку оно противоречит.
Итого bulk 0.373 / n=4 воспроизводимы, а 0.67 — нет.
**Решено (пользователь, 2026-08-17):** baseline критерия 1 — **0.5556 / 0.373 / 4** (последний срез
харнесса); число в `CLAUDE.md` поправить на фактическое из спайка (61 %).

**Второе расхождение — replay физически не может воспроизвести baseline брифов.** Baseline
`predicted` — это пути из `## Relevant code`, которые **написала LLM**, отфильтровав payload
`prepare_task_context` по релевантности. Replay без LLM даёт сырой выход ретрива (все пути из
`code`, возможно ∪ `subsystems`/`test_exemplars`) — другое множество, обычно шире (ниже precision,
выше recall). Значит «replay без изменений воспроизводит baseline» выполнимо только в смысле
«replay задаёт **свою** baseline-линию, а A/B сравнивает replay-с-рычагом против replay-без-рычага».
Развилка для brainstorming:
- (a) replay = чистый ретрив, своя baseline-линия, LLM-брифы остаются отдельной метрикой; или
- (b) replay с LLM-фильтром в петле (дорого, недетерминированно — ломает критерий 4).
**Решено (пользователь, 2026-08-17): вариант (a)** — replay меряет сырой ретрив и задаёт свою
baseline-линию; несравнимость линий replay и brief фиксируется в отчёте явно. LLM-режим не делаем.

**Стоимость и квота Voyage.** Корпус — 44 задачи с ground truth. Каждый replay = ≥2
query-эмбеддинга + rerank на задачу (`code` + `test_exemplars`). Free tier 3 RPM / 10K TPM →
полный прогон троттлится минутами; A/B удваивает. Нужны `--limit`, кэш эмбеддингов запроса и/или
переиспользование `content_hash`. Замер — не CI-шаг.

**Направление зависимости и «только stdlib».** Докстринг `eval/solve_task_metrics/__init__.py`
заявляет «использует только stdlib и никогда не импортируется из `reviewer/**`». Replay обязан
импортировать `reviewer` (`build_components` → Postgres/Neo4j/Voyage). Направление `eval → reviewer`
guard-тестом не запрещено (он ловит только обратное), но заявление про stdlib придётся уточнить, а
импорт живых компонентов — держать **ленивым внутри команды**, чтобы `snapshot|stats|compare|
forecast` продолжали работать без инфраструктуры и без изменения их поведения.

**Воспроизводимость (критерий 4)** зависит не только от коммита: результат определяется состоянием
base-индекса (`indexed_sha`, drift), графа Neo4j и версией реранкера Voyage. Снимок replay обязан
записывать `indexed_sha`/`branch`/`chunks`/`graph_nodes` (всё это уже отдаёт `deps.preflight`),
иначе «тот же коммит» не гарантирует то же число. Отдельно проверить детерминизм реранкера.

**Прочее.**
- `eval/` в `paths.ignore` → ретрив reviewer его не видит; правки в `eval/` через прямое чтение.
- Текст задачи для replay берётся `get_task(key, project)` из стора (116 задач синхронизированы);
  `warm_board` в replay включать не нужно.
- Дедуп по ключу задачи (`snapshot.py:44`) обязателен и в replay: два брифа одного ключа иначе
  дадут задаче двойной вес.
- Проверки: `.venv/bin/pytest -q`; при изменении версии/`plugin/` —
  `scripts/update_codex_plugin_manifest.py`; README.md и README.ru.md — синхронно (guard
  `tests/eval/test_docs.py`).

Собран на: inline (Opus, сессионная модель), режим: inline

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 88 · out 18.5K · cache-write 518.8K · cache-read 3.7M
Всего: 4.3M токенов
