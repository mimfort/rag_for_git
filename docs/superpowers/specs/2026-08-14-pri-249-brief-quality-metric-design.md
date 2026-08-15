# PRI-249 — постоянная метрика качества брифа solve-task

Бриф: `docs/superpowers/briefs/2026-08-14-PRI-249-solve-task-brief-quality-metric.md`

## Задача

Качество ретрива под бриф solve-task не измеряется ничем: правки грунтовки принимаются на глаз.
Спайк PRI-246 (PR #197) показал, что сигнал есть и он не шумовой, а PRI-250 (PR #198) построил
офлайн-харнесс `eval/solve_task_metrics/`, считающий метрику ретроспективно по корпусу брифов и
истории git.

Здесь строится **онлайн**-версия: по факту публикации ревью сопоставить пути секции
`## Relevant code` брифа задачи с файлами, реально изменёнными в PR, посчитать core-recall и
precision, сохранить в Postgres вместе с таксономией промахов и показать динамику в веб-админке.

Критерии приёмки задачи:

1. Метрика считается автоматически, без ручного запуска, и сохраняется в БД.
2. Динамика precision/recall видна в админке.
3. Отсутствие данных не ломает ни ревью, ни solve-task.
4. Метрика хранит core-recall и категорию каждого промаха, а не только сырое число.

Дополнительно: PRI-249 ретроспективно закрывает отложенный критерий 4 задачи PRI-251 (PR #199) —
«core-recall на bulk-подвыборке вырос, числами до/после». База «до» зафиксирована офлайн-харнессом:
`bulk_core_recall_median ≈ 0.373` при `bulk_n_measured = 4` (`eval/solve_task_metrics_history.jsonl`,
коммит `d474e02`). Отсюда сквозное требование дизайна: **онлайн-метрика обязана быть посчитана той
же линейкой**, иначе «до» и «после» несравнимы и критерий останется незакрытым.

## Ключевые решения

### Одно расчётное ядро на офлайн и онлайн

Модули `classify.py`, `recall.py` и `briefs.py` переносятся из `eval/solve_task_metrics/` в новый
пакет `reviewer/metrics/brief_quality/` **дословно**, вместе со своими тестами.
`eval/solve_task_metrics/{classify,recall,briefs}.py` остаются на своих местах как тонкие
ре-экспорты (`from reviewer.metrics.brief_quality.classify import *`), поэтому `__main__.py`,
`snapshot.py`, `report.py` и `forecast.py` продолжают работать без правок.

Так прямо предписывает докстринг `eval/solve_task_metrics/__init__.py`: «эти модули следует
ПЕРЕНЕСТИ в продакшн-слой и импортировать в обе стороны, а не переписать второй раз: общий расчёт
метрик не должен существовать в двух копиях». Паритет линейки с baseline обеспечивается
тождеством кода, а не дисциплиной синхронизации констант.

Переносятся только чистые функции без ввода-вывода. `ground_truth.py` (git через subprocess),
`cost.py`, `history.py`, `snapshot.py`, `report.py`, `forecast.py`, `endtoend.py` остаются в
`eval/`: онлайн-путь git не вызывает и токенами брифа не занимается. Инвариант «`reviewer/**`
никогда не импортирует `eval/**`» сохраняется — направление зависимости становится
`eval/ → reviewer/`.

### Ground truth без git

`PreparedReview` уже несёт всё необходимое:

- `changed_paths: list[str]` — файлы PR;
- `changed_status: dict[str, str]` — статус файла (`modified` / `added` / `removed`).

Отсюда `existed_before(path) ⇔ changed_status.get(path) != "added"`, и знаменатель ядра строится
ровно формулой офлайн-снимка (`eval/solve_task_metrics/snapshot.py:84-88`):

```
expected      = set(changed_paths)
expected_core = {p for p in expected if is_core_production_path(p) and existed_before(p)}
```

Офлайн-харнесс получает тот же ответ через `git cat-file -e <parent>:<path>`; онлайн-путь получает
его от VCS-провайдера бесплатно и не приобретает зависимости от локального клона ради знаменателя.

`predicted` — множество путей секции `## Relevant code` брифа, извлекаемое существующим парсером
`briefs.extract_section_paths` (он же пропускает служебные строки `(dropped N: …)`).
Пути секции `## Test exemplars` в `predicted` не входят — как и офлайн.

### Чтение брифа: канал `repo_clone`

Бриф лежит в репозитории клиента (`docs/superpowers/briefs/*<KEY>*.md`), а считает метрику сервер
`reviewer-mcp`. Единственный существующий канал доступа сервера к файлам клиентского репозитория —
путь к локальному клону из таблицы `repo_clone` (PRI-235): его пишет `reviewer index`, читает
`MCPReviewService._repo_clone_path` → `store.get_repo_clone`, и сейчас им пользуется чтение
коммиченного `.review.yml`.

Метрика использует тот же канал и наследует его ограничение: если сервер работает не на той машине,
где индексировали, клон недоступен и точки измерения просто не будет (`status = "no_brief"`).
Контракт MCP-тула `publish_review` при этом **не меняется** — передача текста брифа с клиента
рассматривалась и отвергнута: она расширяет публичный контракт тула ради случая, который в текущем
деплое (сервер и клон на одной машине) не наступает.

Ключ задачи для поиска брифа берётся из аргумента `task_key` тула `publish_review`, а при его
отсутствии — из `PreparedReview.task_keys["primary"]`, который уже резолвится на этапе prepare.
Нет ни того ни другого → `status = "no_task_key"`.

### Схема хранения

Новая таблица `brief_quality` в `reviewer/web/schema.sql`, одна строка на прогон ревью:

```sql
CREATE TABLE IF NOT EXISTS brief_quality (
    id                 BIGSERIAL   PRIMARY KEY,
    run_id             BIGINT      NOT NULL REFERENCES review_runs (id) ON DELETE CASCADE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    repo               TEXT        NOT NULL,
    pr_number          INT         NOT NULL,
    task_key           TEXT,
    head_sha           TEXT,
    status             TEXT        NOT NULL,   -- measured | no_task_key | no_brief
                                               -- | brief_unreadable | empty_core_denominator
    brief_path         TEXT,                   -- относительный путь брифа в репо
    expected           INT         NOT NULL DEFAULT 0,
    expected_core      INT         NOT NULL DEFAULT 0,
    predicted          INT         NOT NULL DEFAULT 0,
    hit_core           INT         NOT NULL DEFAULT 0,
    core_recall        REAL,                   -- NULL при пустом знаменателе ядра
    raw_recall         REAL,
    precision          REAL,
    misses             JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- категория → счётчик
    predicted_paths    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    expected_core_paths JSONB      NOT NULL DEFAULT '[]'::jsonb,
    hit_core_paths     JSONB       NOT NULL DEFAULT '[]'::jsonb
);
```

Три решения в этой схеме неочевидны и потому выписаны явно.

**`status` отделяет «нет точки измерения» от нулевого recall.** В спайке 10 задач из 45 имели
пустой знаменатель ядра (весь diff — тесты, доки, конфиги). Подмешивать их нулём в медиану значит
систематически занижать метрику; `core_recall IS NULL` при `status = 'empty_core_denominator'`
сохраняет это различие в БД, а не только в памяти расчётного модуля.

**Множества путей хранятся, а не только счётчики.** Офлайн-baseline посчитан по задаче: он
объединяет файлы всех PR-мержей задачи. Онлайн видит по одному PR за раз, и у задачи их может быть
несколько. Без сохранённых множеств task-level объединение невозможно, и «после» оказалось бы
посчитано другой линейкой, чем «до» — то есть критерий 4 PRI-251 остался бы незакрытым. Объём
данных мал (десятки путей на строку).

**Периода «до/после» в схеме нет.** Хранятся `created_at` и `head_sha`; отнесение точки к периоду
до или после PR #199 выводится читателем по дате. Замороженный булев флаг «после family» устарел бы
при следующей правке ретрива, а дата — нет.

Миграция аддитивна и идемпотентна (`CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` по
`(repo, created_at DESC)` и по `task_key`), как остальная схема админки. Бэкфилла нет: исторические
прогоны брифов не сопоставляли, и выдумывать им точки измерения нельзя — ретроспективой занимается
офлайн-харнесс PRI-250.

### Точка съёма

`MCPReviewService.publish_review`, сразу после `_record_history` (`reviewer/mcp/service.py:3044`),
при `not dry_run and posted` и наличии `run_id`. Гейт — существующий `settings.review_history`:
метрика живёт в той же таблице-семье и включается тем же переключателем; отдельный конфиг-ключ не
заводится (YAGNI).

Отдельного события мержа PR нет. Description задачи называл «публикацию ревью или мерж PR»
равноправными триггерами, но мерж серверу не виден без вебхука, которого в системе нет, а diff на
момент публикации ревью — это и есть diff, под который собирался бриф. Доработки после ревью в
метрику не попадут; это осознанное сужение, зафиксированное здесь, а не молчаливое.

Весь блок обёрнут в `try/except` с `log.warning`: любая ошибка метрики не влияет ни на результат
`publish_review`, ни на его отчёт. Отчёт тула не расширяется.

### Компоненты

| Модуль | Роль |
|---|---|
| `reviewer/metrics/brief_quality/classify.py` | `is_core_production_path`, `categorize_miss` (перенос) |
| `reviewer/metrics/brief_quality/recall.py` | `TaskQuality`, `QualityAggregate`, `evaluate_task`, `aggregate`, `BULK_CORE_THRESHOLD` (перенос) |
| `reviewer/metrics/brief_quality/briefs.py` | парсер брифа: `extract_section_paths`, `extract_task_key`, блок токенов (перенос) |
| `reviewer/services/brief_quality.py` | адаптер: найти бриф в клоне → построить множества → посчитать → вернуть `BriefQualityMeasurement` |
| `reviewer/web/history.py` | `record_brief_quality(run_id, row)`, `brief_quality_trend(...)` |
| `reviewer/web/api.py` | `GET /api/quality` |
| `web/frontend/src/pages/Quality.tsx` | страница динамики |

`reviewer/services/brief_quality.py` — единственный модуль с вводом-выводом (чтение файла брифа);
он не знает ни про Postgres, ни про MCP: принимает пути и статусы, возвращает dataclass. Запись в
БД делает `ReviewHistory`, вызов оркеструет `publish_review`. Такое разделение даёт юнит-тесты
расчёта без БД и без файловой системы (кроме `tmp_path` у адаптера).

### Админка

`GET /api/quality?days=90&repo=<owner/name>` возвращает:

- `trend` — точки по времени: дата, `task_key`, `pr_number`, `core_recall`, `precision`,
  `expected_core`;
- `aggregate` — медианы по окну, число измеренных точек и число точек без измерения;
- `bulk` — та же агрегация по подвыборке `expected_core >= BULK_CORE_THRESHOLD`, с константой
  порога в ответе;
- `misses` — суммарная таксономия промахов по окну.

Агрегация по задаче (объединение множеств нескольких PR одной задачи) выполняется на стороне
`ReviewHistory` при чтении — так task-level число совпадает с методикой офлайн-харнесса.

Страница `Quality.tsx` (новый роут в `web/frontend/src/App.tsx`, третья запись рядом с
Dashboard/Runs) рисует на `recharts`: линию core-recall и precision по времени, отдельную линию
bulk-подвыборки с горизонталью baseline `0.373`, и столбчатую разбивку промахов по категориям.
Отдельная страница, а не секция Dashboard: у метрики свои измерения и своё окно, а Dashboard
описывает прогоны ревью.

## Обработка ошибок

Всё дерево fail-soft, каждый отказ — именованный `status`, а не исключение:

| Ситуация | Исход |
|---|---|
| нет `task_key` и `task_keys["primary"]` | строка со `status = "no_task_key"` |
| нет пути клона в `repo_clone`, каталога брифов или файла по ключу | `status = "no_brief"` |
| файл брифа не читается / нет секции `## Relevant code` | `status = "brief_unreadable"` |
| `expected_core` пуст | `status = "empty_core_denominator"`, `core_recall = NULL` |
| несколько файлов брифа на один ключ | берётся лексикографически последний (дата в имени), факт отражается в `brief_path` |
| `record_run` вернул `None` (история недоступна) | метрика не считается вовсе |
| любая непредвиденная ошибка | `log.warning`, строка не пишется, `publish_review` не затронут |

Строки со `status != "measured"` пишутся в таблицу намеренно: «сегодня точки измерения не было и
вот почему» — диагностический сигнал, а молчание неотличимо от «метрика сломалась».

## Тестирование

- Юнит-тесты перенесённых модулей переезжают вместе с кодом: `tests/eval/test_classify.py`,
  `test_recall.py`, `test_briefs.py`, `test_bulk_subsample.py` → `tests/metrics/`. Остальные тесты
  `tests/eval/` (`test_ground_truth`, `test_cost`, `test_snapshot`, `test_endtoend`, `test_forecast`,
  `test_history`, `test_docs`) остаются на месте — их модули не переносятся. Плюс guard-тест на то,
  что `eval/solve_task_metrics` действительно ре-экспортирует продакшн-модуль (иначе копия расчёта
  вернётся незаметно).
- Юнит-тесты `services/brief_quality.py` на `tmp_path`: полный путь, каждый из пяти `status`,
  паритет формулы знаменателя с офлайн-снимком на общей фикстуре.
- Юнит-тесты `ReviewHistory` по образцу `tests/web/test_history.py`: fail-soft без БД;
  integration-тест самолечащейся миграции — под `@pytest.mark.integration`.
- Юнит-тест `/api/quality` через `FakeHistory` (`tests/web/test_api.py:138-171`).
- Тест `publish_review`, что сбой метрики не меняет отчёт тула (`tests/mcp/test_publish.py`,
  `_FakeHistory`).

## Вне скоупа

- Ретроспективный пересчёт по историческим PR — это офлайн-харнесс PRI-250.
- Вебхук на мерж PR.
- Изменение контракта MCP-тулов, включая передачу текста брифа с клиента.
- Автоматический вердикт «качество упало» / алертинг: сначала накопить точки.
- Различение «пропущен файл» и «пропущено N−1 из N однотипных» (сигнал `family`, PRI-251):
  таксономия промахов по категориям это частично покрывает, отдельная механика — за пределами задачи.
