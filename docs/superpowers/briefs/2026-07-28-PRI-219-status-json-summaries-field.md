# Brief — PRI-219 status --json: поле summaries — убрать дамп всех сводок из preflight solve-task
https://ru.yougile.com/team/686c049c8af8/#PRI-219

## Task
- Store-key `ID-272`, alias `PRI-219`, статус «Движок (reviewer CLI/MCP)». Данные — из reviewer-стора после preflight `sync_board` (108 задач, 0 изменений).
- Проблема: `solve-task` Step 0.4 зовёт `get_subsystem_summaries(repo, branch)` без `query` только ради проверки `count == 0`; сервер (`reviewer/mcp/service.py:1064`) отдаёт ВСЕ сводки целиком (~8k токенов на этом репо) в контекст оркестратора, который дальше ведёт brainstorming. **Premise подтверждена в этом же прогоне: вызов вернул 68 882 символа / 226 строк и не влез в контекст.**
- Решение: перенести факт «сводки построены» в уже вызываемый `reviewer status --json` (Step 0.1), где `SummaryStore.count_summaries` уже есть и fail-soft.
- Работы: (1) поле `summaries: int | None` в `BranchStatus`; (2) kw-only `summary_store=None` в `build_status_report` + try/except → None; (3) CLI `status` создаёт/закрывает `SummaryStore`; (4) оба рендера (`"summaries"` в JSON, «Сводки: K» / «—» в тексте); (5) SKILL.md Step 0.4 читает `summaries` из статуса, фолбэк на текущий вызов при `null`/отсутствии ключа; (6) тесты; (7) README.md + README.ru.md; (8) бамп версии + `scripts/update_codex_plugin_manifest.py`.
- Критерии приёмки (6 шт.) — в описании задачи; ключевые: обратная совместимость `build_status_report` без `summary_store`, схлопывание «старый деплой» и «стор упал» в один `null`-путь у потребителя, зелёный `pytest -q` и чистый `ruff` по изменённым файлам.

## Related work
- `ID-141` (done) — solve-task preflight: проверка свежести индекса. Ввёл сам вызов `reviewer status --json` в Step 0.1, который здесь расширяется; следовать его формату отчёта об ошибках (fail-open).
- `ID-167` (done) — векторизация сводок + top-k при масштабе. Ввёл `count_summaries` и `summary_topk_threshold`; переиспользуем ровно этот метод, ничего нового в сторе не нужно.
- `ID-173` (done) — поле `stale` в `get_subsystem_summaries` (read path). Прецедент «добавить поле в read-path сводок с мягким фолбэком у потребителя» — тот же приём, что нужен здесь.
- `ID-161` (done) — solve-task: приор подсистемных сводок перед сбором кода. Ввёл рабочий вызов Step 3 (ANN top-k) — его НЕ трогаем; раздут только preflight.
- `ID-184` (Бэклог) — [solve-task] Freshness guard для subsystem summaries. Будущий потребитель того же места Step 0.4: новое поле должно быть достаточным каналом, чтобы guard не заводил второй вызов.
- (dropped 2: `ID-166` — depth кластеризации, другой механизм; `ID-192` — таймауты на git/uvx в preflight, тот же вызов, но иная забота.)

## Subsystems
- `reviewer/services` — `status.py` строит `RepoStatus`/`BranchStatus`/`OverlayStatus`, чистый слой без Voyage; дрейф через `commits_behind`. Главная точка правки.
- `reviewer/index` — `SummaryStore` (таблица `subsystem_summaries`, idempotent upsert по `source_hash`) и `ChunkStore` (ленивый thread-safe пул psycopg).
- `tests/entrypoints` — CLI-тесты через `CliRunner().invoke` с моком компонентов, без Postgres/Neo4j/сети.
- `tests/index` — тесты `SummaryStore` (integration, требуют `paradedb-test`).
- (dropped 2: `reviewer/graph`, `tests/tools` — задача их не касается.)

## Relevant code
- `reviewer/services/status.py:17` — `@dataclass BranchStatus` (7 полей, все без дефолтов) → добавить `summaries: int | None`.
- `reviewer/services/status.py:49` — `build_status_report(store, graph, repo, branches, repo_path)` → kw-only `summary_store=None`.
- `reviewer/services/status.py:59-62` — образец fail-soft: `try: graph.count_nodes(...) except Exception: None`; копировать один-в-один для `count_summaries`.
- `reviewer/services/status.py:64-66` — конструирование `BranchStatus(...)` (kw-args) — единственное место в проде.
- `reviewer/services/status.py:82-97` — `render_status_json`: словарь per-branch → ключ `"summaries"`.
- `reviewer/services/status.py:126-127` — `render_status`: строка `f"  Чанки:  {b.chunks}   Узлы графа: {nodes}"` — сюда же «Сводки: K», «—» при None (образец форматирования None уже рядом).
- `reviewer/entrypoints/cli.py:590-613` — команда `status`: `ChunkStore` + `GraphStore` в try/finally, `except psycopg.OperationalError → ClickException`. Добавить `SummaryStore(s.pg_dsn)`, передать, закрыть в том же `finally`.
- `reviewer/entrypoints/cli.py:27` — импорт из `reviewer.services.status`; `SummaryStore` в cli пока не импортирован.
- `reviewer/index/summary_store.py:145` — `count_summaries(repo, branch) -> int`, ловит `UndefinedTable → 0`; сетевой сбой пробрасывает → нужен внешний try/except.
- `reviewer/app.py:85-88` — образец конструирования: `SummaryStore(dsn, min_size=…, max_size=…)`.
- `reviewer/mcp/service.py:1055-1064` — серверный `get_subsystem_summaries`: без `query` → `store.get_summaries(...)` (полный дамп). Источник проблемы, **но по задаче не меняется**.
- `plugin/skills/solve-task/SKILL.md:56-71` — Step 0.4 «Summary warmth»: три опции; переписать на чтение `summaries` из статуса.
- `plugin/skills/solve-task/SKILL.md:29-40` — Step 0.1: тот самый `reviewer status --json`, из которого поле и придёт.
- `pyproject.toml:3` — `version = "0.4.0"` → бамп; `scripts/update_codex_plugin_manifest.py` — обязательная пересборка (правка `plugin/` меняет payload-digest).
- `README.md:779` / `README.ru.md:698` — описание потока solve-task (preflight → status → приор сводок); `README.md:653` / `README.ru.md:573` — блок команд диагностики.
- **Blast radius (граф + grep):** `build_status_report` вызывается только из `cli.py:608`; `BranchStatus` конструируется позиционно в 4 местах тестов (`tests/services/test_status.py:68-71, 89, 105-108, 129`) — новое поле обязано быть **последним и с дефолтом `= None`**, иначе тесты падают на арности.
- (dropped 0.)

## Test exemplars
- `tests/services/test_status.py:11-32` — `FakeStore` (get_index_meta_row/count_chunks/list_refs) и `FakeGraph` (`fail=True` → RuntimeError); нужен аналогичный `FakeSummaryStore` с флагом сбоя.
- `tests/services/test_status.py:53-60` — «Neo4j down» → `graph_nodes is None`: точный шаблон теста «стор бросил исключение → поле None».
- `tests/services/test_status.py:35-50` — happy path с `monkeypatch.setattr(status_mod, "commits_behind", …)`.
- `tests/services/test_status.py:63-83` — `render_status`: ассерты по подстрокам вывода («Neo4j недоступен», «не проиндексирована»).
- `tests/services/test_status.py:100-122` — `render_status_json`: `json.loads` + разбор по `{b["branch"]: b}`; сюда добавить `summaries`.
- `tests/services/test_status.py:85-97, 125-139` — CLI smoke/JSON: `monkeypatch.setattr(cli_mod, "ChunkStore"/"GraphStore", MagicMock())`. **Придётся так же замокать `cli_mod.SummaryStore`**, иначе CLI-тесты полезут в реальный Postgres.
- `tests/mcp/test_subsystem_summaries.py:280-313` — образец мока `count_summaries` (`return_value`, `assert_not_called`).
- `tests/index/test_summary_store.py:143-166` — integration-проверка `count_summaries` против реальной БД (менять не нужно, ориентир контракта).
- (dropped 0.)

## Constraints / open questions
- **Ветка индекса:** текущая git-ветка `feature/pri-177` не в `REVIEW_BRANCHES=main,dev`; ретрив шёл по `dev` (drift 0, 4596 чанков), т.к. у формально первичной `main` в индексе 0 чанков. Работу по PRI-219 начинать с новой ветки от `dev`.
- **Бриф собран inline** (Path B): override модели субагента в этой сессии не используется — весь ретрив прошёл в главном контексте.
- `criteria` в сторе пустые (`[]`), но требования полностью лежат в `description` (раздел «## Критерии приёмки») — пробела в требованиях нет.
- **Позиционная арность `BranchStatus`** (см. blast radius) — поле только последним и с дефолтом `None`.
- CLI `status` ловит лишь `psycopg.OperationalError`; `SummaryStore` использует ленивый пул — проверить, что конструктор не коннектится и не роняет команду, когда Postgres жив, а таблицы сводок нет (`UndefinedTable` уже обработан внутри `count_summaries`).
- **Версии расходятся:** `pyproject.toml` = `0.4.0`, а установленный кэш плагина — `0.4.1`. Перед бампом сверить `pyproject` в `dev` (возможно, ветка `feature/pri-177` отстала).
- Задачей **явно отклонены** (не предлагать в brainstorming): `count_only=True` у `get_subsystem_summaries`; отдельный тул `count_subsystem_summaries`; учёт Path B (инлайн-сборка брифа) — вне скоупа.
- Серверный `get_subsystem_summaries` не меняется — экономия достигается на стороне потребителя (скилла).
- README.md и README.ru.md править синхронно (правило репозитория).

Собран на: сессионная модель (Opus 5), режим: inline

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 62 · out 22K · cache-write 271.7K · cache-read 2.2M
Всего: 2.5M токенов
