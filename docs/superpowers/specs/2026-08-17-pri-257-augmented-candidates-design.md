# PRI-257 — Подмешивание фактических diff-путей похожих задач и git-со-изменяемости

Бриф: `docs/superpowers/briefs/2026-08-17-PRI-257-similar-task-diffs-cochange.md`

## Задача

Секция `code` контекста задачи (`prepare_task_context`) ранжируется гибридом по тексту задачи.
Два сильных предиктора в неё не попадают вовсе:

1. **Фактические diff-пути похожих задач.** `related.similar` уже находит похожие задачи, а пути
   их реальных диффов лежат в `brief_quality.expected_core_paths`.
2. **Git-со-изменяемость.** Файлы, исторически менявшиеся вместе с уже найденными.

Оба сигнала — чисто путевые: в контекст сборщика брифа добавляются только пути, тексты задач и
PR туда не попадают, LLM-вызовов не добавляется.

## Решения

### Скоуп — два независимых рычага, замеряемых по отдельности

Сигналы реализуются оба, но включаются независимо и мерятся раздельно: в
`eval/solve_task_metrics/variants.py` заводятся три записи реестра — `similar_paths`, `cochange`,
`similar_paths+cochange`. Критерий приёмки 1 («при непокрывающей дельте рычаг не мержится»)
применяется к каждому рычагу отдельно; код пишется так, что каждый включается сам по себе.

### Источник diff-путей — таблица плюс git-фолбэк

`brief_quality.expected_core_paths` (JSONB, `web/schema.sql:124`) хранит ПОЛНОЕ множество
core-путей диффа PR, а не пересечение с предсказанием: `measure()` кладёт туда `expected_core`
(`services/brief_quality.py:166`). Новая колонка не нужна; индекс `brief_quality_task_key` есть.

Ограничение источника: строка появляется только у задачи, чей PR уже прошёл `publish_review` с
брифом на диске. `review_runs` путей не хранит вовсе (только счётчики), `review_findings.file` —
файлы находок, а не диффа. Поэтому основной источник дополняется фолбэком по git-истории клона:
`git log --grep=<KEY> --name-only` — ключ задачи присутствует в именах веток (`feat/pri-256-…`),
сообщениях merge-коммитов и телах PR. Фолбэк даёт покрытие с первого дня и на репозитории без
накопленной истории прогонов.

Матч ключей идёт по канонической форме И по алиасам: похожие задачи приходят как `ID-311`, а
`brief_quality.task_key` и git-сообщения несут `PRI-257`.

### Квота — жёсткая, по файлам, видимая в выдаче

`CodeSectionLimits.max_augmented_files` (дефолт 3 при `max_files = 12`), читается из
`.review.yml` (`context_limits.code_section.max_augmented_files`) тем же `from_review_yaml`.
Квота общая на оба источника; приоритет при её исчерпании — similar-diffs, затем co-change.

Видимость (критерий 4) — строка-нота в конце секции, рядом с существующими нотами cliff и
degraded: `— подмешано 3 файла: similar-diffs 2, co-change 1 (квота 3)`. Нота попадает и в бриф,
и в replay-отчёт; цена — порядка десятка токенов.

Глубина git-истории для co-change — модульная константа `augment.py`, НЕ ключ политики: третий
регулятор рядом с `max_files`/`max_augmented_files` рассинхронизировался бы с ними, а операторам
крутить его незачем.

## Архитектура

Порядок источников в `search_multi` — `hybrid (RRF) → graph-only → augmented`; далее без
изменений `_dedupe_overlapping → diversify_by_file → cap_block`. Позиция augmented последняя,
поэтому при полном бюджете гибрид вытесняет их естественно, а квота страхует обратный случай —
бедную гибридную выдачу.

### Новые единицы

| Единица | Что делает | От чего зависит |
|---|---|---|
| `store.fetch_retrieved_at_paths(repo, paths, *, base_ref, limit_per_path)` | пути → `Retrieved`; симметричен `fetch_nodes` (`store.py:519`), но ключ — путь. Отдаёт самый широкий чанк файла | Postgres |
| `reviewer/retrieval/augment.py` | чистый слой: `collect_similar_task_paths(...)`, `rank_cochanged(commit_file_sets, seeds, *, min_count, limit)`, `AugmentResult(paths, by_source, gaps)` | ничего (I/O внедряется параметрами) |
| `gitutil.commit_file_sets(repo, *, limit)` | последние N коммитов как «коммит → множество файлов», один процесс git | git |
| `gitutil.paths_touched_by_grep(repo, pattern, *, limit)` | пути коммитов, чьё сообщение содержит ключ задачи | git |
| `History.diff_paths_for_tasks(keys, repo)` | `task_key → set(expected_core_paths)`, union по всем строкам задачи | Postgres |
| `TaskService.search_hits(query, project, top_k)` | структурные `TaskHit` (с новым полем `aliases`); `search_tasks` становится рендером поверх | Postgres |
| `CodeSectionLimits.max_augmented_files` | квота | `.review.yml` |
| `ContextPack.augment_note` | нота видимости; печатается в `as_context` рядом с cliff/degraded | — |

`rank_cochanged` — чистая функция: она получает уже прочитанные множества файлов коммитов, а не
путь к репозиторию. Так подсчёт со-появления тестируется без git и без временных репозиториев.

### Точки интеграции

`search_multi(..., augment_paths=None, cochange=None)`:

* `augment_paths` — готовый список путей похожих задач; от выдачи не зависит, считается снаружи.
* `cochange` — callable `seeds → paths`; co-change по определению считается ОТ найденного, но git
  внутрь `multiquery.py` не тащим — модуль остаётся без внешнего I/O, кроме стора.

`_TaskContextDeps.code` (`mcp/service.py:3557`) собирает вход: ключи похожих задач, историю через
`_ensure_history()`, клон через `_clone_path()`. Ключи берутся из хитов, сохранённых вызовом
`deps.similar` в том же прогоне: `build_task_context` вызывает `similar` (строка 104) до `code`
(строка 110), и порядок закрепляется тестом — второй поиск означал бы второй эмбеддинг и лишний
расход квоты Voyage (3 RPM / 10K TPM).

### Обработка ошибок

Каждый источник — свой `try/except` с `log.warning`, по образцу `_graph_items`
(`multiquery.py:104-119`). Недоступные Postgres/история/git не прерывают сборку: пути этого
источника пусты, а в payload добавляется `gap("code.augment", <причина>)`. Отсутствие
`REVIEW_HISTORY` выключает табличный источник само собой — строк просто нет; git-фолбэк при этом
продолжает работать.

## Тестирование

Юниты (без сети и БД):

* `rank_cochanged` — подсчёт со-появления, порог `min_count`, лимит, отсутствие seed'ов.
* Квота: augmented не занимают больше `max_augmented_files` файлов; приоритет similar-diffs над
  co-change при исчерпании квоты.
* Порядок: hybrid вытесняет augmented при полном бюджете (по образцу существующего
  `test_graph_only_tail_yields_to_hybrid_files`, `tests/retrieval/test_multiquery.py:282`).
* Инвариант «dedupe → diversify» не нарушен (существующий тест остаётся зелёным).
* Fail-soft (критерий 3): недоступные `brief_quality` / git по отдельности и вместе дают `gaps`,
  а не исключение — по образцу `tests/mcp/test_prepare_task_context.py:102,161`.
* Матч ключа через алиасы: `ID-311` находит строки с `task_key='PRI-257'`.
* Нота: формат и наличие в `as_context`.
* Порядок вызовов `similar` → `code` в `build_task_context` (ключи долетают до `search_multi`).

Приёмка (критерий 1) — replay-харнесс на том же `indexed_sha`, три варианта реестра против
baseline, дельта bulk core-recall фиксируется в `eval/replay_report.md` отдельным разделом.

Замечание для живой приёмки: установленный пакет `rag-reviewer` 0.5.0 отдаёт секцию `code` ещё
однозапросным путём (в выдаче видна cliff-нота), то есть без изменений PRI-255/256. Живая
проверка требует переустановки пакета из `dev`; на unit- и replay-приёмку это не влияет.

## Границы

* Публичные `search_codebase` / `search_base` не трогаются — путь остаётся параллельным, как и
  после PRI-255.
* Секция `subsystems` не затрагивается: разворот кластеров в файлы-кандидаты — параллельный
  рычаг PRI-312.
* Формат `payload.related.similar` не меняется — меняется только способ его получения.
* Схема БД не меняется: миграций нет, новых колонок нет.
