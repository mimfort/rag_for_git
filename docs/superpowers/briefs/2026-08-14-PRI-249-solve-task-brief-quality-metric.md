# Brief — PRI-249 Постоянная метрика качества брифа solve-task: попадание ретрива в реально изменённые файлы PR
https://ru.yougile.com/team/686c049c8af8/#PRI-249

## Task
PRI-249 (ID-303, Бэклог): по факту `publish_review`/мержа PR сопоставлять пути `## Relevant code` брифа с реально изменёнными файлами, считать **core-recall** (знаменатель — только существовавший продакшн-код) и precision, персистить в БД с таксономией промахов, показать динамику в `reviewer/web/`, fail-soft при отсутствии брифа/ключа/связи. Гейт «сигнал не шумовой» снят спайком PRI-246 (45 задач: сырой recall медиана 15% — артефакт знаменателя, core-recall медиана 67%; провал на bulk воспроизводим: PRI-134 8%, PRI-223 24%, PRI-225 26%, PRI-215 36%). Метрика обязана считать core-recall с самого начала, не сырой. `criteria=[]` в сторе — критерии взяты из текста description (5 пунктов).

## Related work
- PRI-250 (ID-304, **done, PR #198, коммит `10ea008`**) — офлайн-харнесс `eval/solve_task_metrics/` УЖЕ РЕАЛИЗОВАН (не план — см. Constraints); переиспользовать его чистые функции (`classify.py`, `recall.py`, `cost.py`), не переписывать — так завещано докстрингом `eval/solve_task_metrics/__init__.py`.
- PRI-246 (ID-300, done, PR #197) — спайк-предшественник, источник методологии/весов/baseline; `eval/pri246_report.md` — образец разбивки промахов.
- **PRI-251 (ID-305, done, PR #199) — прямой предшественник по механизму ретрива; PRI-249 ретроспективно закрывает его отложенный критерий 4.** PR #199 дал class-level `IMPLEMENTS` (tree-sitter) + тул `family` (структурный сигнал для `typing.Protocol`) — прямая починка причины низкого core-recall на bulk-задачах. Критерий 4 PRI-251 («core-recall на bulk-подвыборке вырос до/после, числами») явно отложен: база «до» зафиксирована (`bulk_core_recall_median≈0.373`, `bulk_n_measured=4`), рост измерим только после накопления брифов, решённых уже с `family`. Онлайн-метрика PRI-249 — механизм, который со временем это докажет.
- PRI-127 (упомянута в description, не найдена под текущим ключом в поиске) — архитектурный прецедент «персистентная метрика обратной связи + БД + админка», не более.
(dropped 2: ID-209/ID-161/ID-153/ID-138 из search_tasks — про качество ретрива в целом или relevance-score выдачи инструментов, не про постфактум-сверку с реальным diff PR.)

## Subsystems
- `reviewer/services` — сервисный слой подготовки/синхронизации ревью; сюда логически ляжет вычисление метрики.
- `reviewer/agent` — паттерн полного учёта воронки (`len(rows)==len(candidates)`) для промахов.
- `reviewer/policy` — `.review.yml`/`ContextLimits`-паттерн для будущего конфиг-ключа метрики.
- `reviewer/index` (`summary_store.py`) — образец «идемпотентный upsert + fail-soft при отсутствии таблицы».
(Приор слабый: сводки не покрывают `reviewer/web/`, `reviewer/tasks/`, `eval/`. dropped 4 tests/*-кластера.)

## Relevant code
- `reviewer/mcp/service.py:931-1096` (`publish_review`) — **точка съёма**: уже принимает опциональный `task_key`, при `posted and task_key` (1047-1056) вызывает `link_review(task_key, pr_ref, p.changed_node_ids)`. `p.changed_node_ids`/`p.changed_paths` — готовый список изменённых файлов, git заново парсить не нужно. Новый вызов метрики встаёт рядом с `_record_history` (1068).
- `reviewer/tasks/service.py:399-406` (`TaskService.link_review`) — образец fail-soft контракта (`if graph is None or not task_key: return`) для новой функции.
- `reviewer/tasks/graph.py:110-166` (`link_pr`, `task_context`) — граф хранит `(:Task)-[:IMPLEMENTED_BY]->(:PR)-[:TOUCHES]->(:Symbol)`; развилка: ground truth синхронно из `p.changed_node_ids` при publish, либо ретроспективно из `TOUCHES` — синхронный путь дешевле git-парсинга PRI-250.
- `reviewer/web/history.py:21-430` (`ReviewHistory`) — ленивая инициализация схемы, `record_run` — образец транзакционной записи; новая таблица метрики повторяет структуру `review_findings` (JSONB для таксономии, как `usage`/`config_sources`).
- `reviewer/web/schema.sql:1-91` — образец идемпотентной миграции (`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN IF NOT EXISTS` + бэкфилл, 61-70) — прямой прецедент для новых колонок.
- `reviewer/web/api.py:20-143` (`make_router`) — `_require_auth`, `GET /api/runs|stats` — новый `/api/quality` тем же паттерном.
- `reviewer/web/app.py:33-125` (`create_app`) — SPA-fallback; регистрации нового роута достаточно в `make_router`.
- `web/frontend/src/pages/Dashboard.tsx:1-60` — `recharts`/`KpiCard`/цветовые константы — шаблон для тренда core-recall/precision и разбивки промахов.
- `web/frontend/src/App.tsx:36-40` — регистр роутов React Router, новая страница — третья запись.
- `reviewer/config/committed.py:53-104` (`CommittedLayerFetcher`) + `reviewer/mcp/service.py:1365-1381` (`_repo_clone_path`) + `reviewer/index/store.py:209-222` (`get_repo_clone`) — **ключевой прецедент**: сервер уже читает файлы клиентского репо (`.review.yml`) из локального клона по пути из `repo_clone` (PRI-235), с graceful fallback. Закрывает главный открытый вопрос — доступ к `docs/superpowers/briefs/`.
- `plugin/skills/solve-task/SKILL.md:238-265` — «Relevance filter»: `## Relevant code` без фикс. лимита, обязательная строка `(dropped N: reason)` — парсер обязан её игнорировать (уже решено PRI-250).
- `eval/solve_task_metrics/briefs.py:1-180` — готовый парсер (`extract_task_key`, `extract_section_paths`).
- `eval/solve_task_metrics/classify.py:1-47`, `recall.py:1-88` — готовые чистые функции `is_core_production_path`, `categorize_miss`, `evaluate_task`, `aggregate`, константа `BULK_CORE_THRESHOLD=10` (`recall.py:13,81-84`).
(dropped 0: все фрагменты — прямая точка интеграции, источник переиспользуемого кода или образец паттерна.)

## Test exemplars
- `tests/web/test_history.py:84-100,125-153,176-212` — fail-soft без БД + `@pytest.mark.integration` self-healing миграции — образец для новой таблицы.
- `tests/web/test_api.py:138-171` (`FakeHistory`) — фейк вместо БД для unit-тестов `/api/*`.
- `tests/mcp/test_publish.py:35-45,130-142` (`_settings`, `_FakeHistory`) — мокинг `ReviewHistory` внутри теста `publish_review`.
- `tests/eval/` (PRI-250) — юнит-тесты core-recall/classify уже существуют; при переносе кода переносятся вместе.
(dropped 1: целевой `include_tests=True`-запрос не добавил новых файлов сверх найденного.)

## Constraints / open questions
- `criteria=[]` в сторе — критерии только текстом в description.
- **PRI-250 уже смержен (PR #198, `10ea008`), а не «план для чтения»** — context gap относительно исходной постановки. Разграничение: PRI-250 = офлайн/ретроспективный/по всему корпусу/git log --merges/CLI. PRI-249 = онлайн/событийный на publish_review/ground truth из `p.changed_node_ids`/графа `TOUCHES`/Postgres/`reviewer/web/`. Общее расчётное ядро не должно существовать в двух копиях (докстринг `eval/solve_task_metrics/__init__.py`).
- **Доступ к брифам с сервера подтверждён по коду**: единственный канал — `repo_clone`-путь (PRI-235), сейчас только для `.review.yml`. Без общего клона (сервер на другой машине/в контейнере) брифы физически недоступны → деградация в «нет точки измерения», не падение. Альтернатива — клиент передаёт текст брифа в `publish_review` (меняет контракт тула, решение вне скоупа брифа).
- **Импорт `eval/` из `reviewer/**` запрещён явным правилом PRI-250** (и обратно) — переиспользование = перенос кода в `reviewer/`; направление/расположение — открытый вопрос дизайна.
- Таксономия промахов из спайка — закладывать как категориальное/JSONB поле, словарь уже в `classify.categorize_miss`.
- 10/45 задач в спайке — пустой core-знаменатель → отдельное состояние `core_recall IS NULL`, не 0.
- Провал на bulk (PRI-134/223/225/215) воспроизводим — смежно с `family` (PRI-251); проверить различение «пропущен файл» vs «пропущено N-1 из N однотипных».
- **PRI-249 обязана закрыть отложенный критерий 4 PRI-251 (до/после `family` на bulk-подвыборке) той же линейкой, что PRI-250.** Формула (`recall.py:13,81-84`): `BULK_CORE_THRESHOLD=10`, bulk = `expected_core >= 10`, `bulk_core_recall_median` = медиана `core_recall` по подвыборке. Baseline «до» (`eval/solve_task_metrics_history.jsonl`, коммит `d474e02`): `bulk_core_recall_median≈0.373`, `bulk_n_measured=4`. Схема БД обязана хранить: (а) дату/коммит точки замера + маркер периода до/после PR #199 (`b911c52`); (б) сам `expected_core` для фильтра `>=10` той же границей; (в) `core_recall` по той же классификации `classify.py` — иначе «до»/«после» несравнимы. Общий код между `eval/` и `reviewer/` НЕ гарантирован (разные среды: офлайн git-based vs онлайн без прямого git-доступа) — если перенос констант не сделан, документировать вручную синхронизируемые `BULK_CORE_THRESHOLD`/`classify.py` как риск рассинхронизации, не решать молча.
- Событие съёма — `publish_review` (уже видит `task_key`+`changed_node_ids` синхронно), не отдельное «событие мержа»; description называет оба триггера равноправными — решить, достаточно ли одного publish_review, учитывая доработку/задержку мержа после публикации ревью.

Собран на: mid (sonnet), сборка: subagent
