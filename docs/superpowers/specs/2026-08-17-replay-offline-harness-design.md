# Replay-режим офлайн-харнесса метрик solve-task (PRI-254)

Дата: 2026-08-17. Задача: ID-308 / PRI-254.
Бриф: `docs/superpowers/briefs/2026-08-17-PRI-254-replay-offline-harness.md`.

## Задача

`eval/solve_task_metrics/` меряет ретроспективно: `briefs.load_briefs` парсит уже написанные брифы
и сопоставляет пути их секции `## Relevant code` с ground truth (PR-мержи по ключу задачи). Прогнать
по тому же корпусу новый алгоритм ретрива невозможно — брифы фиксированы, поэтому дельта рычагов
PRI-253 (ID-307, ID-311, ID-312) непроверяема.

Нужен режим `replay`: для каждой задачи корпуса заново собрать кандидатов вызовом продакшн-пути
ретрива, сопоставить с ground truth той же линейкой и сравнить два варианта конфигурации в одном
отчёте.

## Что меряет replay (и чего не меряет)

**Replay меряет сырой ретрив, а не бриф.** Baseline существующего харнесса построен на путях,
которые **LLM отобрала** из payload `prepare_task_context` по релевантности. Replay без LLM в петле
даёт полный выход ретрива — другое, более широкое множество. Поэтому:

- replay задаёт **свою** baseline-линию и сравнивает replay-с-рычагом против replay-без-рычага;
- линии `replay` и `snapshot` **несравнимы напрямую**; отчёт replay пишет это явно;
- LLM-фильтр в петле сознательно не делается: он недетерминирован (ломает воспроизводимость) и
  стоит один LLM-вызов на задачу на каждый прогон A/B.

Опубликованный baseline харнесса — последний срез `eval/solve_task_metrics_history.jsonl`
(коммит `d474e02`): `core_recall_median` **0.5556** (N=35), `bulk_core_recall_median` **0.373**
(`bulk_n_measured` **4**). Число 0.67 из формулировки тикета не воспроизводится ни одним артефактом
репозитория; спайк `eval/pri246_report.md` даёт 61 %, `CLAUDE.md` — 67 %. Расхождение чинится в
`CLAUDE.md` (67 % → 61 %) в рамках этой задачи.

## Состав множества `predicted`

`predicted` = пути из секции **`code`** payload'а `prepare_task_context`, и только они.

Обоснование: знаменатель `expected_core` по определению содержит только продакшн-код
(`classify.is_core_production_path`), тестовые пути в него не попадают. Подмешивание
`test_exemplars` роняло бы precision, ничего не давая recall. Секция `subsystems` отдаёт
`cluster_key`-каталоги, а не файлы; их разворот в файлы — предмет отдельной задачи ID-312, и
включать его в линейку значит мерить рычаг им же самим.

`test_exemplars` в replay **не собирается вовсе**. Знаменателя для него нет: `expected_core`
исключает тесты по построению, а заводить второй знаменатель ради справочной строки значит платить
второй запрос Voyage на задачу и вводить ещё одну линейку. Если понадобится — добавляется отдельной
задачей.

## Извлечение путей: из отрендеренного текста, не из объекта

`ContextPack.as_context` (`reviewer/retrieval/retriever.py:94`) обрезает вывод по
`max_context_chars` с хвостом `[...truncated]`, а также добавляет хвостовые заметки (cliff, degraded).
Часть найденных ретривом путей до сборщика брифа не доезжает.

Поэтому replay извлекает пути **из того же текста, который получает LLM** — из заголовков
`// <node_id> (<path>:<start>-<end>)`. Чтение `ContextPack.items` напрямую приписало бы ретриву
кандидатов, которых сборщик брифа не видел, и завысило бы метрику.

## Архитектура

Шесть новых модулей в `eval/solve_task_metrics/`, разделённых по тому, что каждому нужно для работы.

| Модуль | Роль | Зависимости |
|---|---|---|
| `context_paths.py` | Парсер путей из отрендеренного вывода ретрива | stdlib |
| `variants.py` | Реестр стратегий сборки кандидатов + парсер `--set` | stdlib |
| `replay.py` | Оркестрация прогона корпуса и сборка снимка | stdlib + ре-экспорты ядра |
| `replay_history.py` | Схема и хранилище снимков replay | stdlib |
| `replay_report.py` | Отчёт A/B (агрегат + дельта по задачам) | stdlib |
| `live.py` | Единственная точка живого импорта `reviewer` | reviewer, PG, Neo4j, Voyage |

**`replay.py` не знает про `live.py`.** Источник кандидатов инъектируется так же, как `run_git`
инъектируется в `build_snapshot` (`snapshot.py:20`), поэтому вся логика прогона тестируется на
фейковом провайдере без инфраструктуры.

**`live.py` импортируется лениво, внутри тела команды.** Подкоманды `snapshot|stats|compare|forecast`
продолжают работать без Postgres, Neo4j и Voyage, и их поведение не меняется.

Докстринг `eval/solve_task_metrics/__init__.py` уточняется: stdlib-инвариант закрепляется за
расчётными модулями, `live.py` объявляется единственным исключением. Инвариант «`reviewer/**` не
импортирует `eval/**`» остаётся нетронутым (guard `tests/metrics/test_reexport_guard.py:29`).

`variants.py` живёт в `eval/`, а не в `reviewer/`: продакшн-путь про эвал знать не должен.

## Провайдер ретрива (`live.py`)

Интерфейс, который `replay.py` получает инъекцией:

```
class RetrievalProvider(Protocol):
    def preflight(self, repo: str, branch: str) -> dict: ...
    def task(self, key: str) -> dict | None: ...
    def code(self, repo: str, branch: str, query: str, limits: dict | None) -> str: ...
```

Живая реализация собирает `Settings()` → `build_components(settings)` → `MCPReviewService` и вызывает
**продакшн-методы** `service.search_codebase(...)`, `service.get_task(...)` и
`build_status_report(...)` — те же, что дёргает `_TaskContextDeps` (`reviewer/mcp/service.py:3480`).
Доска не трогается: `warm_board` в replay не выполняется.

Запрос ретрива строится **той же функцией**, что в проде — `reviewer.mcp.task_context._query`
(title + первые 8 строк description) и `_test_query`. Копии формулы запроса не заводится: это тот же
класс дефекта, что PRI-249 запрещает для формул метрики. Функции приватные; `replay.py` их не
импортирует — импорт живёт в `live.py`, рядом с остальными живыми зависимостями.

Компоненты закрываются в `finally` (пул Postgres, драйвер Neo4j).

### Варьирование лимитов

`limits` — словарь оверрайдов в форме блока `context_limits` из `.review.yml`; `live.py` сворачивает
его через существующий `ContextLimits.from_review_yaml` и передаёт в `retriever.search_base`.
Второго парсера лимитов не появляется.

## Реестр вариантов (`variants.py`)

Вариант — именованная стратегия, возвращающая множество путей-кандидатов по задаче:

```
@dataclass(frozen=True)
class TaskInput:
    key: str          # ключ задачи корпуса
    task: dict | None # нормализованная задача из стора (None → задачи в сторе нет)
    query: str        # запрос ретрива, построенный продакшн-формулой

Variant = Callable[[RetrievalProvider, TaskInput, ReplayTarget], set[str]]
# ReplayTarget — frozen dataclass (repo, branch, limits: dict | None)
```

Регистрируются ровно два (YAGNI — абстракция под будущие рычаги не строится, но точка расширения
есть):

- `baseline` — пути из `code` при дефолтных лимитах;
- `limits` — то же с оверрайдами из `--set ключ=значение` (`--set search_codebase.ceiling=25`).

Рычаги ID-311/312 добавят свои стратегии одной строкой реестра. Неизвестное имя варианта — ошибка с
перечислением доступных, а не тихий фолбэк на baseline.

## Поток данных прогона

1. **Корпус.** `briefs.load_briefs(BRIEFS_DIR)` → ключи задач, дедуп по ключу (два брифа одного
   ключа иначе дают задаче двойной вес — инвариант из `snapshot.py:44`). Корпус replay определяется
   тем же множеством ключей, что и корпус `snapshot`, — иначе линии несравнимы даже между собой.
2. **Ground truth.** `ground_truth.collect(key, run_git)` → `changed`, `parent_ref`; `expected_core`
   строится ровно как в `snapshot.py:84`, с тем же кэшем `existed()`.
3. **Вход задачи.** `provider.task(key)` — текст задачи из стора reviewer, не из брифа. Запрос
   ретрива — `_query(task, key)`.
4. **Кандидаты.** `variant(provider, task_input, limits)` → множество путей.
5. **Метрика.** `recall.evaluate_task(key, predicted, expected, expected_core)` —
   ре-экспортированное продакшн-ядро, второй копии нет.
6. **Агрегат.** `recall.aggregate(rows)`.

### Статусы задачи (отказ всегда именован)

Молчаливый пропуск неотличим от «метрика сломалась» — тот же принцип, что в
`reviewer/services/brief_quality.py:94`. Каждая задача корпуса получает статус:

| Статус | Смысл |
|---|---|
| `measured` | посчитано, `core_recall` — число |
| `empty_core_denominator` | знаменатель ядра пуст, `core_recall = None` (не ноль) |
| `no_ground_truth` | PR-мержа по ключу не нашлось |
| `task_not_in_store` | задачи нет в сторе reviewer |
| `retrieval_failed` | ретрив упал на этой задаче |

Прогон не прерывается ни на одном из них; счётчики статусов входят в снимок.

## Снимок replay (`replay_history.py`)

Своя схема и свой файл `eval/replay_history.jsonl` — формат `snapshot` агрегатный, а критерий 2
требует дельту **по задачам**, поэтому смешивать их в одну историю нельзя.

Снимок содержит:

- **Идентичность прогона:** `schema`, `taken_at`, `commit`, `variant`, `variant_params`,
  `indexed_sha`, `branch`, `chunks`, `graph_nodes`, `partial`.
- **Агрегат:** `core_recall_median`, `core_recall_mean`, `bulk_core_recall_median`,
  `bulk_n_measured`, `n_measured`, `no_measurement`, `raw_recall_median`, `precision_median`,
  `predicted_median` (медианы по задачам со статусом `measured`), счётчики статусов.
- **Построчно по задачам:** ключ, статус, `expected`, `expected_core`, `predicted`, `hit_core`,
  `core_recall`, `precision`, отсортированные `predicted_paths` и `expected_core_paths`.

Пути хранятся множествами, а не только счётчиками, — по той же причине, что и в `brief_quality`
(PRI-249): без них дельта по задачам не показывает, **какие** файлы вариант приобрёл и потерял.

**Воспроизводимость (критерий 4)** определяется не только коммитом: результат зависит от состояния
base-индекса и графа. Поэтому `indexed_sha`, `chunks` и `graph_nodes` — часть идентичности снимка, и
при сравнении их несовпадение даёт явное предупреждение в отчёте. Тихо склеивать снимки с разных
состояний индекса запрещено — тот же принцип, что `WINDOW_MODE` в `snapshot.py:13`.

**`--limit N` помечает снимок `partial: true`.** Частичный снимок нельзя использовать как
`--baseline` — команда отказывает явно. Это тот же fail-closed, что у `sync_board`, где `--limit`
отключает продвижение курсора.

## Отчёт A/B (`replay_report.py`)

Markdown по образцу `report.render` (`report.py:19`), записывается в
`eval/replay_report.md` и печатается сводкой в stdout.

Секции:

1. **Идентичность** обеих сторон: вариант, параметры, коммит, `indexed_sha`, размер корпуса,
   `partial`. Несовпадение `indexed_sha`/`commit` — предупреждение отдельной строкой.
2. **Агрегат с дельтой:** таблица «метрика | baseline | variant | Δ» по `core_recall_median`,
   `bulk_core_recall_median`, `bulk_n_measured`, `precision_median`, `predicted_median`,
   `n_measured`.
3. **Дельта по задачам:** таблица «ключ | статус | core-recall до | после | Δ | +файлы | −файлы»,
   отсортированная по модулю дельты — чтобы сразу было видно, на каких задачах вариант сработал, а
   на каких сломал. Задачи без изменения сворачиваются в одну строку-счётчик.
4. **Оговорка о несравнимости** линий `replay` и `snapshot`.

Одиночный прогон (без второй стороны) печатает те же секции без колонок дельты.

## CLI

Новая подкоманда, существующие не трогаются:

```
python -m eval.solve_task_metrics replay [--variant NAME] [--set KEY=VALUE ...]
                                         [--baseline SNAPSHOT] [--limit N]
                                         [--repo OWNER/NAME] [--branch BRANCH]
```

- `--variant` (дефолт `baseline`) — имя из реестра.
- `--set` — оверрайд лимитов для варианта `limits`, повторяемый.
- `--baseline` — переиспользовать сохранённый снимок как сторону «до»; без флага и при
  `--variant != baseline` обе стороны гоняются в одном процессе.
- `--limit N` — усечь корпус; помечает снимок `partial`.
- `--repo` / `--branch` — дефолты из `DEFAULT_REPO` и первичной ветки.

**Прогон обеих сторон в одном процессе дёшев по квоте Voyage:** `VoyageEmbedder.embed_query`
(`reviewer/index/embeddings.py:162`) держит потокобезопасный LRU-кэш, поэтому вторая сторона
переиспользует эмбеддинги тех же запросов. Платится только реранк. Троттлинг покрывается существующим
`with_voyage_retry`; собственного sleep-слоя не заводится.

## Тестирование

Юнит (без инфраструктуры, `tests/eval/`):

- `test_context_paths.py` — парсер заголовков: обычный вывод, `(ничего не найдено)`, хвост cliff,
  `[...truncated]` посреди заголовка, degraded-заметка.
- `test_variants.py` — реестр, парсер `--set`, ошибка на неизвестном варианте и на неразбираемом
  оверрайде.
- `test_replay.py` — прогон на фейковом провайдере и инъектируемом `run_git`: все пять статусов,
  дедуп по ключу, `--limit` → `partial`, идентичность агрегата с `recall.aggregate`.
- `test_replay_history.py` — round-trip снимка, отказ использовать `partial` как baseline.
- `test_replay_report.py` — дельта по задачам и агрегату присутствует; предупреждение при
  расхождении `indexed_sha`.

Guard-тесты:

- `tests/metrics/test_reexport_guard.py` остаётся зелёным без правок (критерий 3).
- `tests/eval/test_docs.py:16` расширяется: `replay` документирован в `README.md` и `README.ru.md`.
- Новый guard: `replay.py` не импортирует `reviewer` — проверка по форме оператора импорта, как в
  существующем guard'е направления зависимости.

Integration (`@pytest.mark.integration`): прогон `replay --limit 3` на живом деплое — проверяет, что
`live.py` собирает компоненты, закрывает их и отдаёт непустые пути.

## Документация

- `README.md` и `README.ru.md` — подкоманда `replay` в разделе харнесса (синхронно, guard-тест).
- `CLAUDE.md` — исправить 67 % на фактические 61 % из спайка PRI-246.

## Вне скоупа

- LLM-фильтр в петле replay.
- Стратегии рычагов ID-307/311/312 — здесь только точка расширения.
- Изменение поведения `snapshot|stats|compare|forecast`.
- Прогон replay в CI: замер упирается в квоту Voyage и требует живого индекса.
