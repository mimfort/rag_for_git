# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## О проекте

`rag_for_git` — агент автоматического ревью pull/merge request'ов: **RAG (гибридный поиск) + граф кода + Claude Code-плагин**. На вход — PR на GitHub, на выход — inline-комментарии на строки диффа + сводка. Целевой язык анализа — Python; VCS — GitHub (за интерфейсом `VCSProvider`). Подробный разбор архитектуры и потока данных — в `README.md` (написан на русском, сверен с кодом).

Язык проекта — **русский**: комментарии, докстринги, сообщения CLI. Сохраняй этот стиль в новом коде.

## Команды

```bash
# Инфраструктура: ParadeDB (5433) + Neo4j (7687)
docker compose up -d

# Конфиг: ключи Voyage/GitHub
cp .env.example .env        # .env gitignored, ключи только локально

# Тесты
# unit: без Postgres, Neo4j, localhost-сервисов и внешней сети
.venv/bin/pytest -q
# изолированная инфраструктура integration-тестов
docker compose --profile test up -d --wait paradedb-test neo4j-test
# integration; пайплайну также нужен VOYAGE_API_KEY
.venv/bin/pytest -q -m integration
# только безопасное удаление
docker compose --profile test rm -sfv paradedb-test neo4j-test

# Линт
git config core.hooksPath .githooks   # один раз на клон: pre-commit гоняет ruff по staged .py

# CLI (после pip install -e .)
reviewer check --board-project jira=PRI           # проверить окружение и project-scoped board permissions
reviewer index /path/to/repo --ref main          # построить/обновить base-индекс (вектора + граф) из локального клона
reviewer index /path/to/repo --ref master        # индекс второй ветки (изолированный, тот же деплой)
reviewer search "token verification"              # диагностический гибрид-поиск по base-индексу (первичная ветка)
reviewer search "token verification" --branch master  # поиск по индексу конкретной ветки
reviewer status                                   # здоровье/свежесть индекса по веткам (не тратит Voyage)
reviewer status /path/to/repo --branch dev        # статус конкретной ветки (дрейф vs git HEAD клона)
reviewer migrate-branches                         # одноразовая миграция legacy base → base:<primary> после апгрейда
reviewer serve                                    # веб-админка наблюдаемости на хосте (история прогонов, находки)

# MCP-сервер (для Claude Code-плагина)
reviewer-mcp                               # запустить MCP-сервер (используется плагином)

# Ревью через Claude Code-плагин:
# 1. Открыть репозиторий как проект в Claude Code
# 2. Использовать скилл /rag-reviewer:review-pr (из plugin/)
#    Плагин вызывает prepare_review → analyze (Claude subagents) → publish_review

# На хосте (для разработки фронта): pip install -e ".[web]" && (cd web/frontend && npm install && npm run build) && reviewer serve
```

Обычный `pytest` не запускает инфраструктуру и по умолчанию **исключает** integration-тесты
(`addopts = -m 'not integration'` в `pyproject.toml`). Unit-тестам запрещены внешние и
localhost-сокеты. Любой тест с реальной сетью обязан иметь `@pytest.mark.integration`.

DB integration-тесты используют `TEST_PG_DSN`, `TEST_NEO4J_URI`, `TEST_NEO4J_USER` и
`TEST_NEO4J_PASSWORD`. Значения `TEST_*` никогда не должны совпадать с эндпоинтами dev- или
production-сред. Сервисы Compose для разработки и тестов различаются портами, учётными данными
и хранилищем. Тестовые данные хранятся в `tmpfs`, а образы тестовых сервисов зафиксированы по digest.

Никогда не используй `docker compose --profile test down -v`: тестовые сервисы и сервисы разработки
входят в один проект Compose, поэтому команда удалит контейнеры разработки и именованные тома.
Безопасна только адресная команда
`docker compose --profile test rm -sfv paradedb-test neo4j-test`.

## Архитектура

Ядро — библиотека `reviewer/`, собираемая в `reviewer/app.py::build_components(settings)` из `Settings` (pydantic-settings, `.env`). Точка входа — `reviewer/entrypoints/cli.py` (Click) и `reviewer/entrypoints/mcp_server.py` (FastMCP).

Поток ревью: **prepare_review** (MCP) → **analyze** (Claude subagents через скилл `/rag-reviewer:review-pr`) → **publish_review** (MCP).

1. **prepare** — `ReviewService.prepare`: `GitHubProvider` тянет PR (base/head sha), изменённые `.py` чанкуются (tree-sitter) и эмбеддятся (Voyage) в `ref="pr:N"`. Возвращает `PreparedReview` с юнитами, policy, patches.
2. **analyze** — Claude Code-скилл (fan-out на subagents): каждый файл ревьюится параллельно с инструментами `search_code`, `get_related_symbols`, `read_file`, `get_definition`, `find_callers`, `get_changed_file_diff` через MCP.
3. **publish** — `MCPReviewService.publish_review`: gate (политика/severity/confidence) → grounding строки по code_quote → dedup → assemble → публикация inline + сводки; запись истории; очистка overlay/сессии.

MCP-сессия (PreparedReview + ToolContext) живёт в процессе `reviewer-mcp` между вызовами `prepare_review` и `publish_review` одного PR. Плагин = корень репозитория (`plugin/`).

Граф LangGraph удалён; MCP-сервер определён в `reviewer/entrypoints/mcp_server.py` через FastMCP.

### Модули

| Модуль | Роль |
|---|---|
| `reviewer/vcs/` | `VCSProvider` + `github.py` (httpx); `diff.py` — какие строки диффа доступны для inline |
| `reviewer/index/` | `chunker` (tree-sitter) · `embeddings`/`reranker` (Voyage) · `store` (pgvector + pg_search/BM25, RRF) · `freshness` (base/overlay) |
| `reviewer/graph/` | `builder` (tree-sitter call-graph) · `scip` (парсер SCIP) · `backend` (оркестратор бэкенда: SCIP / tree-sitter) · `store` (Neo4j) |
| `reviewer/retrieval/` | `Retriever`: гибрид (RRF) + graph-expansion + Voyage rerank → `ContextPack` |
| `reviewer/llm/` | `_retry.py` (retry/backoff для Voyage) |
| `reviewer/tools/` | инструменты MCP-агента PR-сессии (`search_code`, `get_related_symbols`, `read_file`, `get_definition`, `find_callers`, `get_changed_file_diff`); session-less варианты для Q&A — `search_codebase`/`related_symbols`/`callers`/`definition`/`implementations`/`family` в `mcp/service.py` |
| `reviewer/agent/` | `state` (ReviewUnit) · `assemble` · `dedup` |
| `reviewer/mcp/` | `MCPReviewService` — сервисный слой MCP (prepare/tools/publish/history) |
| `reviewer/services/` | `ReviewService.prepare` — подготовка PR (ingest + overlay + policy + units) |
| `reviewer/policy/` | `ReviewPolicy`: env-дефолты + `.review.yml` из целевой ветки + гейтинг |

### Ключевые инварианты

- **`node_id = "path#fqn"`** — единый ключ связи RAG↔граф. И чанк в Postgres (`store.py`), и узел в Neo4j используют его, поэтому graph-expansion и ретрив чанков сшиваются без маппинг-таблицы.
- **Свежесть индекса (base + overlay).** `ref="base:<branch>"` — персистентный индекс целевой ветки (инкрементальный, дедуп по `content_hash`). `ref="pr:N"` — эфемерный overlay изменённых файлов PR. На запросе ретрив = `(base:<branch> где path ∉ changed) ∪ overlay` — для изменённых файлов агент видит новую версию, для остального стабильную базу. Логика в `index/freshness.py` и `WHERE`-условиях `store.hybrid_search`/`fetch_nodes`.
- **inline только на строках диффа.** GitHub разрешает комментарии лишь на изменённых/контекстных строках хунка; остальное уходит в сводку (зашито в `assemble`, см. `commentable_lines`).
- **applyable `suggestion`-блок** ставится только при безопасных инвариантах (`_can_apply` в `assemble.py`): режим `apply`, точная замена, весь диапазон в RIGHT-части диффа, без пересечений. Иначе — текстовый совет.
- **Идемпотентность** — каждый комментарий помечен скрытым фингерпринтом `<!-- ai-review:hash -->`; повторный прогон не плодит дубликаты.

## Неочевидные факты (не выводятся из кода)

- **ParadeDB слушает host-порт 5433**, а не 5432 (на машине разработчика 5432 занят нативным Postgres). `PG_DSN` по умолчанию указывает на 5433.
- **Граф кода — два бэкенда.** Оркестратор `graph/backend.py` выбирает бэкенд через `GRAPH_BACKEND` (auto|scip|treesitter):
  - **SCIP** (`scip-python`, npm `@sourcegraph/scip-python`) — точный type-aware граф, рёбра CALLS и IMPLEMENTS, требует `scip-python` в PATH. Индексация выполняется через временный git worktree на `ref` (`add_worktree`/`remove_worktree` в `gitutil.py`).
  - **tree-sitter** (`graph/builder.py`) — быстрый fallback без внешних зависимостей: `CALLS` по имени и class-level `IMPLEMENTS` из синтаксиса (PRI-251, см. ниже); метод-уровневых `IMPLEMENTS` не даёт.
  - Режим `auto` (по умолчанию): если `scip-python` найден в PATH — SCIP, иначе tree-sitter. При `backend=scip` сбой пробрасывается; при `auto` — откат на tree-sitter с `log.warning`. Команда `reviewer index` полностью перестраивает граф (clear + upsert), чтобы рёбра разных бэкендов не смешивались.
- **Наследование классов в графе приходит из tree-sitter, а не из SCIP (PRI-251).**
  scip-python 0.6.6 не эмитит `SymbolInformation` для класса, упомянутого в файле
  ВЫШЕ своего определения, — а значит и `si.relationships` у такого класса нет,
  читать нечего. В этот провал попадают все 11 адаптеров досок: каждый
  регистрируется в `provider_spec()`, объявленной до класса. Измерено на
  репозитории: из 185 классов с `SymbolInformation` forward-referenced — 0, из 14
  без неё — 13. Поэтому `reviewer/graph/inherit.py` извлекает `class X(Y)`
  синтаксически, а `build_with_scip` сливает эти рёбра с рёбрами SCIP
  (дедупликация). SCIP остаётся источником точных `CALLS` и метод-уровневых
  `IMPLEMENTS`. Дефект upstream закреплён integration-тестом: если новая версия
  scip-python начнёт эмитить символ, тест покраснеет.
- **`local N`-символы SCIP файл-скоупные, и глобальная карта давала фикцию (PRI-252).**
  Идентификатор `local <N>` в index.scip уникален только внутри своего документа:
  `local 0` в `reviewer/app.py` и `local 0` в `tests/web/test_pool.py` — один и тот
  же ключ. `parse_scip` до PRI-252 клал все definition-символы в общий
  `symbol_to_node`, документы перетирали друг друга, и ссылка на локальное имя в
  одном файле резолвилась в определение из другого. Измерено на этом репозитории:
  14942 фиктивных ребра `reviewer/* → tests/*` из 30254 CALLS — 49 %. Теперь
  `local`-символы резолвятся в пределах своего документа (`_is_local` в
  `graph/scip.py`); внутрифайловые рёбра при этом сохраняются.
  Побочное следствие того же дефекта — число рёбер зависело от окружения запуска
  `scip-python`: worktree своего окружения не имеет, pyright резолвит типы по
  окружению вызывающего процесса, и чем хуже резолв, тем больше символов остаются
  `local`. На одном коммите `.venv` проекта давал 30254 CALLS, системный python —
  32832, `uvx` — 34158, каждое значение детерминировано в своём условии. После
  фикса остаточное расхождение — 0.5 % (17776 против 17683); поэтому детектор
  просадки (`graph/metrics.py`) сравнивает с порогом 10 %, а не на равенство.
- **Семейство символов (`family`) — не то же, что `implementations`.**
  `implementations` отвечает «кто наследует X» по рёбрам графа. `family` отвечает
  «кто ещё такой же» и добавляет второй сигнал — структурное покрытие набора
  методов контракта с учётом унаследованных. Он нужен потому, что `typing.Protocol`
  (`TaskBoardProvider`, `VCSProvider`) рёбер наследования не даёт ни при каком
  бэкенде: структурная типизация не выражается рёбрами. На этом репозитории
  структурный сигнал находит все 11 адаптеров (включая три легаси без общей базы)
  и оба VCS-провайдера, без ложных срабатываний. Пустой ответ при существующем
  семействе запрещён: `implementations` в этом случае явно отсылает к `family`.
  Структурный сигнал не считается вовсе при контракте меньше `MIN_CONTRACT_METHODS`
  (3 не-dunder метода) — на тонком контракте (0-2 значимых метода) совпадение имён
  ничего не значит, а ложных срабатываний было бы больше, чем пользы. Пропуск
  сигнала не тихий: он называется в шапке ответа (`family.py::FamilyResult.note`).
- **Voyage free tier = 3 RPM / 10K TPM.** TPM — главный блокер: полный `reviewer index` большого репо упирается в лимит и троттлится; есть retry/backoff (`index/_retry.py`). Ревью одного PR (overlay + query-эмбеддинги) в лимит укладывается.
- **`.review.yml` берётся из целевой (base) ветки**, не из PR — PR не может ослабить собственное ревью.
- **Реестр досок и `task_board`.** `BoardProviderRegistry` — единственная точка расширения: каждый зарегистрированный immutable `BoardProviderSpec` описывает factory, credential schema, setup/validation metadata и runtime options. Generic MCP, `SyncService`, `Settings` и installer не ветвятся по provider type. `.review.yml` целевой ветки выбирает зарегистрированный `task_board.type`, `project`, generic `create_target`/`done_target` и non-secret `options`; пустой `task_board:` выключает доску для репо. Новая форма имеет приоритет над legacy mapping (`done_column`/`done_state` → `done_target`, `status_field` → `options.status_field`) в течение одного compatibility-релиза. Креды приходят только из server-side env через `ProviderCredentialSource`; `board_config()` и MCP ошибки их не возвращают. См. `docs/board-providers.md`.
- **Одиннадцать зарегистрированных типов досок** (PRI-217): `yougile`, `youtrack`, `jira`, `github`, `trello`, `linear`, `clickup`, `asana`, `yandex_tracker`, `kaiten`, `weeek`. Новые адаптеры строятся на общем транспорте — `boards/restbase.py` (`RestBoardBase`: lifecycle клиента, вымарывание секретов, разделение read/write), `boards/pagination.py` (offset / page / cursor / `Link`-заголовок), `boards/graphql.py` (`GraphQLClient` для Linear), `boards/yfm.py` (YFM → markdown для Yandex Tracker); retry, `Retry-After` и категоризация статусов остаются в `BoardHttpClient`, провайдер добавляет лишь `rate_limit_hint`. **Долг:** три исходных адаптера (`yougile`, `youtrack`, `jira`) старше этого слоя и держат свою httpx-обвязку — ретрофит сознательно вне скоупа. Ключи: нативные у `linear`/`yandex_tracker`, у остальных синтезируются из option `key_prefix` (нативный id — в `RawTask.board_id`/`aliases`). OAuth loopback не поддерживается нигде (плагин работает в headless-CLI и по SSH) — только PAT/API-ключ и `help_url`.
- **Болк-синк задач — server-side ETL, не LLM (`sync_board`).** Скил `sync-tasks` — тонкий триггер generic lifecycle: `sync_board(..., board_type, provider_options)` резолвит зарегистрированный тип, проверяет безопасную credential schema, создаёт provider с immutable options и гарантированно вызывает `close()`. `reviewer-mcp` перечисляет доску по REST за полным `TaskBoardProvider` контрактом, нормализует в `TaskBrief` и индексирует через `TaskService.index_batch`; LLM не перечисляет доску и не передаёт текст задач → O(1) токенов. Watermark ключуется `ref="tasks:<type>:<board>"`; повторный синк трогает ~0 задач, а `--limit` отключает purge и продвижение курсора. Задачи глобальны (таблица `tasks` и граф `:Task` без repo-скоупа), но синк и выдача скоупятся `task_board.project`; клиент передаёт этот project, сервер repo-агностичен.
- **SHA base-индекса** хранится в таблице `index_meta` (пишется при `reviewer index`). При каждом `prepare_review` (MCP) SHA сравнивается с `base_sha` PR и при расхождении автоматически досинхронизируются чанки изменившихся файлов через GitHub compare API. Граф (Neo4j) **также** инкрементально досинхронизируется в этом шаге (tree-sitter, repo-scoped, входящие `CALLS`/`IMPLEMENTS`-рёбра сохраняются, исходящие переустанавливаются, fail-soft). Class-level `IMPLEMENTS` (PRI-251) self-heal восстанавливает и тогда, когда база наследования лежит в НЕизменённом файле: перед пересборкой берётся дешёвый снимок уже существующих в графе символов (`GraphStore.all_node_ids`) и подмешивается как дополнительный источник резолвинга — только для баз наследования, не для `CALLS` (`services/graph_sync.py`). Дыра остаётся один случай — базы нет в графе вовсе (репозиторий никогда не индексировался целиком, либо и база, и наследник появляются в одном PR, а база при этом сама не входит в изменённые self-heal'ом файлы); полная точность метод-уровневых `IMPLEMENTS` (SCIP) восстанавливается только ручным `reviewer index`.
- **Мульти-бранч base-индекс.** Отслеживаемые ветки репозитория резолвятся слоями (`reviewer/config/branches.py::resolve_repo_branches`): домашний per-repo файл `home:repos/<owner>/<name>.yml` → домашний глобальный `home:review.yml` → env `REVIEW_BRANCHES` (CSV, первая — первичная) → `["main"]`; `RepoBranches.source` показывает, какой слой сработал (`reviewer config show` печатает эффективные ветки и источник). Каждая отслеживаемая ветка имеет изолированный base-индекс: в Postgres `ref="base:<branch>"` (overlay PR остаётся `pr:N`), в Neo4j `:Symbol{repo, branch, id}` (составная уникальность `(repo, branch, id)`). PR ревьюится против индекса своей целевой ветки (`prq.base_ref`); PR в ветку вне списка ревью **пропускает** (`prepare_review` → `{"status":"skipped",...}`). `reviewer index --ref <branch>` строит индекс ветки; `reviewer search --branch <branch>` ищет по нему. Эмбеддинги переиспользуются между ветками по `content_hash` (экономия Voyage). Ветка-агностичные операции (CLI search, solve-task) идут по первичной ветке или текущей git-ветке клона. Миграция legacy env-данных в домашний слой: `reviewer config migrate` (per-repo) или `reviewer migrate-branches` (один раз после апгрейда).
- **Сбой чтения коммиченного `.review.yml` не обнуляет домашние слои.** `resolve_policy_data`
  (`reviewer/config/layers.py`) читает коммиченный слой fail-soft: сбой доставки (сеть, токен, 404 —
  категория `unavailable`) и сбой разбора (`malformed`) пропускают слой, пишут структурную запись в
  `ResolutionMeta.skipped` и продолжают резолв, поэтому `home:review.yml` и
  `home:repos/<owner>/<name>.yml` применяются. Строгость включается отдельным флагом
  `strict_committed` и выставлена в `True` только там, где тихая потеря политики недопустима:
  ревью PR (`services/review_service.py`), `reviewer index` (`entrypoints/cli.py`) и миграция
  конфига (`config/layers.py`). MCP `_resolve_policy` намеренно мягкий — три его потребителя уже
  откатываются на env-дефолты, и строгий режим заставил бы их потерять домашний слой. Диагностик
  бессекретный: слой, репо, ref, категория, транспорт, HTTP-код — никаких URL, заголовков и
  токенов (классификация формы исключения — `config/fetch_errors.py`). `reviewer config show`
  печатает эффективные значения и блок `skipped`, но код возврата остаётся `1`. Слияние слоёв
  рекурсивно по mapping-значениям (`config/deepmerge.py`): лист затеняется листом, а не соседи по
  верхнему ключу — `task_board` единственный атомарный mapping-ключ (сливается целиком, не по
  подполям), списки, скаляры и явный пустой mapping всегда заменяются целиком, а не сливаются
  поэлементно: `context_limits: {}` — высказывание слоя «секции нет», а не молчание. `sources` и
  `shadowed` в `config show` называют путь до листа (`context_limits.code_section.max_files`), а
  не верхний ключ — иначе «подсекция потеряна» неотличимо от «её и не было». Метка способа чтения
  коммиченного слоя — `git-blob` (коммиченный объект git на ref, а не файл рабочего дерева);
  расхождение рабочего дерева клона с этим блобом печатается отдельной строкой `worktree_drift` с
  перечислением разошедшихся листовых ключей (без значений) и на эффективную политику не влияет —
  сравнение проводится только когда коммиченный слой реально прочитан из клона
  (`committed_source == git-blob`) и `ref` резолвится в тот же коммит, что HEAD клона, иначе
  статус `ref_not_head` явно называет причину пропуска. Разошедшийся текст при совпадающих
  значениях (комментарий, переформатирование) — это `clean`, а не `drifted` без ключей; статус
  `unknown` (сбой самой диагностики) в текстовом выводе молчит, оставаясь в `--json`.
- **Коммиченный слой читается из локального клона, а не из API хостинга (PRI-235).**
  `config/committed.py::CommittedLayerFetcher` — единственный фетчер коммиченного `.review.yml`
  для `config show` и MCP `_resolve_policy`: при пригодном клоне это `git show <ref>:.review.yml`
  (ноль сетевых вызовов; VCS-провайдер вообще не создаётся — фабрика ленивая), иначе прежний
  `vcs.get_file_at_ref`. `config show` берёт клон из `--path`, иначе из текущего каталога
  (в Postgres за путём не ходит — диагностика не должна зависеть от живой БД); MCP берёт его из
  таблицы `repo_clone`, которую пишет `reviewer index` (он и так выполняется из клона, рядом с
  `set_repo_vcs`). Ни один путь не принимается на слово: `validate_clone` требует git-репо и
  сверяет remote с целевым repo, **но клон без распознаваемого remote принимает** — ради него
  задача и делалась. Ref проверяется `rev_parse` ДО чтения: иначе «ветка не выкачана в клоне»
  неотличимо от «файла нет на ref» и молча обнулило бы слой вместо фолбэка на VCS. Способ чтения
  виден в отчёте (`committed: git-blob|vcs`, ключ `committed_source` в JSON); путь к клону в
  диагностику не попадает.
- **Мульти-репо через `repo`-дискриминатор**: один деплой обслуживает N репозиториев через `repo` (`owner/name`) в Postgres (`chunks`/`index_meta`) и Neo4j (`:Symbol.repo`, составная уникальность `(repo, branch, id)`). Индексация: `reviewer index --repo owner/name` (или derive из git remote `origin` / `DEFAULT_REPO`). Граф задач `:Task` глобален — задача может покрывать несколько микросервисных репозиториев. Каждое ревью изолировано в рамках своего репо (без кросс-репо ретрива).
- **`reviewer check`** проверяет готовность окружения (ключи, Postgres, Neo4j, GitHub) без трат квот Voyage.
- **Резолв repo-тега сообщает происхождение.** `resolve_repo_id` (`services/repo_id.py`) возвращает
  `RepoResolution(repo, source)` со словарём источников `cli` | `git:origin` | `env:DEFAULT_REPO`
  (тот же словарь, что у `RepositoryDetection` в `config/onboarding.py`). Функция принимает уже
  прочитанный URL origin, а не путь, — модуль остаётся без git/subprocess и тестируется без моков.
  `reviewer index` **fail-closed** при `env:DEFAULT_REPO`: индекс под чужим тегом обнаруживается
  только по странной выдаче поиска, поэтому отказ идёт до единой записи в хранилища (обход — явный
  `--repo`). `status` и `migrate-branches` остаются fail-open и лишь показывают источник:
  `RepoStatus.repo_source`, ключ `repo_source` в `status --json` и предупреждение в тексте.
- **Layout кластеризации сводок (`SUMMARY_CLUSTER_DEPTH`, дефолт 2).** Default depth и per-prefix `summary_cluster_depth_overrides` из effective `.review.yml` образуют canonical `layout_token` (нормализованные и отсортированные overrides). Смена любого компонента token → **полный пересбор всех сводок и пофайловых fragments**, даже если default depth не изменился. `subsystem_summary_state` хранит `completed_depth` для диагностики и nullable `completed_layout` как identity завершённого прохода; legacy row без token считается незавершённым. Осиротевшие данные вычищаются только verified `prune_subsystem_summaries` после полного uncapped прохода (PRI-166/PRI-226).
- **Инкрементальные fragments сводок (`/summarize-subsystems`).** Первый полный прогон после обновления bootstrap-ит все текущие файлы, не удаляя старые сводки до успешной атомарной замены каждого cluster bundle; при cap bootstrap продолжается в следующих проходах. Server-owned `_reviewer` stamp содержит generation, `layout_token` и diagnostic depth; только exact same-cluster path/fingerprint/stamp доказывает completion. Дальше LLM читает и суммаризирует по одному job только `added_files + changed_files`, а composer получает сохранённые/перенесённые/новые fragment-тексты без исходников; несколько exact cross-cluster кандидатов считаются ambiguous и регенерируются. Fingerprint строится по skeleton-коду: body-only правки намеренно не меняют freshness. Частичный/capped прогон и optimistic race (`ready=false`/`stored=false`) считаются deferred/raced и не запускают prune. Полный проход передаёт list `layout_token` и exact source-hash map; service re-derive и store-проверка summary/fragment coverage под advisory lock предшествуют любому delete/state advance. Backfill использует CAS по `source_hash + title + summary` и считает только успешные записи. Отчёт суммирует `created`, `reused`, `removed`, `moved`, `deferred`/`raced`, `fragments_pruned` и `embedded`.
  Перечисление кластеров идёт в **сжатом режиме с пагинацией** (`list_subsystem_clusters(...,
  compact=True, limit=N, offset=M)`): по кластеру только метаданные и числовые счётчики
  `added`/`changed`/`removed`/`moved` — без путей и fingerprint'ов, размер O(числа кластеров), а
  не файлов (на этом репозитории 10 922 Б в сжатом режиме против 97 530 Б в полном; до PRI-229
  полный формат весил 106 878 Б). File-level детализация — через `get_subsystem_summary_work`.
  В полном формате `files` содержит только
  неизменённые файлы: пути delta-списков в нём не дублируются, полный состав =
  `files ∪ added_files ∪ changed_files ∪ moved_files`. Пагинация не считается override'ом —
  полный проход требует лишь дойти до `has_more == false`.
- **Фильтр кластеризации сводок (`summary_paths.ignore`, PRI-245).** Отдельный от `paths.ignore`
  слой: `paths.ignore` управляет индексацией и ревью, а этот ключ — только кластеризацией сводок.
  Дефолт `("tests", "test")`; env-слоя нет (как у `context_limits`), явный пустой список
  выключает фильтр. Применяется при сборке members ДО `build_clusters` в обеих точках —
  `_summary_state` и `_current_subsystem_hashes`; расхождение наборов сделало бы каждую сводку
  вечно stale. Входит в payload `layout_token`, поэтому включение/выключение запускает полный
  пересбор и штатный `prune_subsystem_summaries` собирает осиротевшие `tests/*`.
- **Вход файлового job сводок — скелет, а не исходник (PRI-245).** `skeleton_hash` считается по
  тексту символа-чанка, поэтому «читать ровно то, что инвалидирует» достижимо только чтением из
  чанков: session-less тул `get_file_skeletons(repo, paths, branch)` собирает скелет файла как
  объединение скелетов его чанков. Цена — module-level docstring в чанки не попадает и в скелет
  не войдёт. `read_file` остаётся тулом PR-сессии и session-less аналога не имеет. Job'ы
  батчатся по 15 путей — один субагент на порцию, а не на файл.
- **Overlay удаляется автоматически** (`store.delete_ref("pr:N")`) — после `publish_review` эфемерный
  ref не остаётся в Postgres. При сбое prepare также чистится (fail-soft). Но если ревью **брошено**
  между `prepare_review` и `publish_review` (пользователь отменил, оркестрирующая LLM-сессия упала),
  публикация не вызывается вовсе — такой overlay собирает **GC** (`reviewer/services/gc.py`):
  оппортунистически при каждом `prepare_review` и по команде `reviewer gc`. Сирота = `pr:N` без
  живой строки в `review_sessions` и вне активных сессий процесса.
  Живость — по последней активности (PRI-212, keepalive): обращения к сессии бампают
  `last_seen_at` (in-memory всегда; в Postgres — `SessionStore.touch`, не чаще 60 с),
  предикат везде `COALESCE(last_seen_at, created_at)` внутри TTL `review_session_ttl_hours`
  (idle-таймаут, единый для GC, регидрации и `delete_expired`) — активное ревью дольше TTL
  не теряет overlay, брошенное собирается как прежде.
  GC никогда не трогает `base:<branch>`; при недоступной БД не удаляет ничего
  («не знаю живых» ≠ «живых нет»). Гарантию даёт только сервер: скилл `review-pr` — это промпт,
  а не `try/finally`.
- **Наблюдаемость (`reviewer/web/`)**: каждый `publish_review` пишет в Postgres итоги прогона (`review_runs`/`review_findings`, гейт `REVIEW_HISTORY`) — fail-soft. Веб-админка (FastAPI `reviewer serve`) читает **ту же** БД. В колонке `review_runs.config_sources` сосуществуют две формы ключей: строки до PRI-260 хранят верхние ключи политики (`"paths"`), строки после — листовые пути (`"paths.ignore"`); маркера версии у строки нет, поэтому аналитика по этой колонке обязана учитывать обе формы.
- **Онлайн-метрика качества брифа solve-task (PRI-249).** По факту реальной публикации ревью
  (`publish_review`, только `posted and not dry_run`) пути секции `## Relevant code` брифа задачи
  сопоставляются с фактическим diff'ом PR и пишутся в таблицу `brief_quality` рядом с историей
  прогонов. Три вещи в этом неочевидны. Во-первых, **знаменатель — не весь diff**: считается
  core-recall по ядру (`reviewer/**/*.py`, `plugin/**` не-`.md`, корневые `*.py`) И только по
  файлам, существовавшим до PR; сырой recall на том же корпусе давал медиану 18 % против 61 %
  у core (числа спайка `eval/pri246_report.md`), то есть был метрикой размера diff'а, а не
  качества ретрива.
  «Существовал до PR» берётся из `PreparedReview.changed_status`, git при съёме не вызывается.
  Во-вторых, **пустое ядро — это `status='empty_core_denominator'` и `core_recall IS NULL`, а не
  ноль**: у задачи, чей diff состоит из тестов и доков, качество ретрива по ядру не определено
  (в спайке таких 10 из 45). В-третьих, **строка хранит множества путей, а не только счётчики**:
  офлайн-baseline посчитан по задаче (объединение всех её PR), онлайн видит по одному PR, и без
  union'а на чтении task-level число было бы посчитано другой линейкой, чем точка «до»
  (`bulk_core_recall_median ≈ 0.373`, `bulk_n_measured = 4`) — то есть отложенный критерий
  PRI-251 остался бы незакрытым. Расчётное ядро одно на офлайн и онлайн:
  `reviewer/metrics/brief_quality/`, а `eval/solve_task_metrics/{classify,recall,briefs}.py` —
  ре-экспорт (guard-тест ловит возврат второй копии). Гейт — общий `REVIEW_HISTORY`; своего
  ключа у метрики нет. Мержа PR метрика не видит: вебхука в системе нет, и правки после ревью
  в неё не попадают — сознательное сужение.
  **Рядом существует офлайн-only `context-recall` (PRI-261) — вторым знаменателем, не заменой.**
  Он покрывает файлы, которые надо ПРОЧИТАТЬ, а не изменить (core-recall их структурно не видит:
  ни в recall, ни как штраф precision), знаменатель — контекстное ядро из графа (соседи по
  исходящим `CALLS`/`IMPLEMENTS`, один хоп от символов, затронутых диффом), считает только
  `eval/solve_task_metrics/replay.py`; в `brief_quality` не пишется и в онлайн-снимок
  (`build_snapshot`/`report.py`) не попадает. **Гейт не пройден**: ручная сверка дала 41.8 % против
  предзарегистрированного порога ≥ 50 % (41/98 путей на 8 задачах, `eval/replay_report.md`,
  раздел «PRI-261 — отрицательный результат») — метрика не готова к решениям. Размер знаменателя
  решает сидирование, а не глубина обхода: сидирование всеми символами изменённого файла даёт 57
  новых core-файлов на хоп, сидирование только затронутыми диффом символами — 14.5 на том же
  графе и глубине (спайк на `indexed_sha 308b86b`); тот же урок повторился ещё ниже, при разборе
  провала гейта — сидирование от ЦЕЛОГО символа, внутри которого лежит хунк, а не от изменённых
  строк, тащит несвязанный call-граф на комментарийных/help-текст правках (PRI-227, PRI-236) и
  заражает от god-модулей типа `mcp/service.py`/`cli.py`, где один хунк в файле подмешивает
  call-граф другой, несвязанной функции того же файла (PRI-215, PRI-221). Глубина фрагмента
  (`chars_per_file`) на знаменатель в принципе не влияет — не как эмпирический факт, а как
  доказательство по коду: `diversify_by_file` (`reviewer/retrieval/multiquery.py`) фиксирует набор
  файлов только по `max_files`/`max_chunks_per_file` ДО того, как `cap_block` обрежет текст уже
  отобранных блоков по `chars_per_file` — набор путей физически не может зависеть от глубины,
  поэтому никакая метрика, считающая пути, не отличит `780` от `975` от `1300` (три прогона дали
  тождественные все медианы агрегата и побайтово идентичные пути; расходится только `core_recall_mean` — от пола шума харнесса, не от глубины). Решение — не чинить на этой ветке: гейт не
  пройден, follow-up (сузить сиды до символов с содержательно изменёнными строками) зафиксирован
  как гипотеза post-hoc, не как поправка к вердикту.
  **Follow-up PRI-262 выполнен, и гейт взят: 64.0 % против порога ≥ 50 %** (`eval/pri262_eye_check.md`,
  предзарегистрация — `eval/pri262_preregistration.md`, раздел «Приёмка PRI-262» в
  `eval/replay_report.md`). Сидирование теперь строчное, и неочевидны в нём три вещи. Во-первых,
  **значимость хунка решается содержанием, а не позицией**: сравниваются левый и правый блоки с
  вымаранными строковыми литералами и комментариями, потому что `help="..."` внутри декоратора —
  это код, и позиционное правило пропустило бы ровно тот случай (PRI-236), ради которого задача
  делалась. Во-вторых, **фильтр по именам с изменённых строк** (`allowed_names` в
  `derive_context_core`) чинит god-модули, которых сужение сида не чинит вовсе: мусор там приходил
  рёбрами, исходящими из НЕТРОНУТЫХ строк задетой функции. `None` (фильтра нет) и пустое множество
  («вызовов на изменённых строках нет») — намеренно разные вещи; слить их значило бы вернуть весь
  обход там, где сказать нечего. В-третьих, цена размена названа и не сглажена: осмысленные пути
  23 → 16, мусорные 76 → 9, у 11 задач знаменатель стал неопределённым, медиана размера ядра 0.
  Метрика точнее и уже одновременно. Отдельно стоит знать, что сторона «до» в отчёте PRI-262
  читается как 23.2 %, а не как опубликованные PRI-261 41.8 %: этот проход не засчитывает файл,
  которого на момент задачи не существовало (`boards/asana.py` добавлен на день ПОЗЖЕ мержа
  PRI-215), — числа двух отчётов сравнимы каждое внутри себя, но не между собой.
  **PRI-266 вернула часть покрытия, и её гейт взят по обеим половинам: покрытие 31 → 33 задачи с
  измеренным знаменателем, точность 58.6 % при пороге ≥ 50 %** (`eval/pri266_eye_check.md`,
  предзарегистрация — `eval/pri266_preregistration.md`, раздел «Приёмка PRI-266» в
  `eval/replay_report.md`). Механизмов два, и они независимы. Первый — **шапка символа-сида как
  второй источник `allowed_names`**: декораторы, аннотации параметров, дефолты, аннотация
  возврата, базы классов; сами сиды при этом НЕ расширяются, а имена параметров в множество не
  идут (они локальны и ребром графа не выражаются). Источник имён — рантайм-режим
  (`--context-seeds lines|lines+signature`, дефолт после приёмки — `lines+signature`), а не
  правка кода между замерами: обе стороны A/B обязаны сниматься одним исходником. Второй —
  **отдельный статус `undefined_context_denominator`**: у задачи, чьё изменённое ядро целиком
  не-Python, сидов не может возникнуть ни при какой настройке фильтра, поэтому её знаменатель
  неопределим, а не пуст, и решается это по фактическим путям задачи, а не по результату обхода
  (иначе неопределимость маскируется пустой выдачей графа). На корпусе таких 14 из 65. Три вещи
  в итоге неочевидны. Во-первых, **предзарегистрированное предсказание сигнатурной гипотезы не
  подтвердилось**: PRI-251 не вернула ни одного из трёх поимённо потерянных путей
  (`index/chunker.py`, `index/models.py`, `gitutil.py`) — те зависимости назывались в телах
  НЕТРОНУТЫХ строк, куда шапка не дотягивается; взято покрытие, а не гипотеза. Во-вторых,
  **точности рычаг не добавил**: подмешанные им пути осмысленны на четверть (1 из 4 на выборке),
  доля осмысленных сместилась 64.0 % → 58.6 % — внутри предзарегистрированного пола шума (±1 файл
  на задачу), но вниз. Характерный мусор — `reviewer/vcs/base.py`, подмешанный в 4 задачи из 7
  изменившихся: типы из аннотаций половины кодовой базы шапка тянет независимо от темы задачи.
  В-третьих, **вся правка живёт в `eval/`**: `reviewer/metrics/brief_quality/**` не тронут, и
  аддитивность подтверждена не только тестом — вся core-линия обеих сторон побайтово совпадает.
- **Ядро метрики брифа репо-агностично, а съём — трёхточечный (PRI-271/PRI-270).**
  Ядро продакшн-путей, ключ задачи и каталог брифов перестали быть хардкодом
  rag_for_git: `reviewer/metrics/brief_quality/config.py::BriefQualityConfig`
  резолвится из `.review.yml` (ключ `metrics.brief_quality.core_paths`, см. этот
  файл в корне репозитория) и передаётся явным параметром `config` во все функции
  расчётного ядра — без дефолта, чтобы молчаливое ядро rag_for_git не оказалось
  тихим провалом для чужого репозитория. Три вещи здесь неочевидны. Во-первых,
  **`unconfigured_core_denominator` требует ДВУХ условий одновременно** — пустой
  знаменатель ядра И отсутствие ключа `core_paths` в `.review.yml` — а не одного
  из них: развязать их значило бы столкнуть лбами «в диффе только тесты и доки»
  (честный `empty_core_denominator`) и «репозиторий не настроен, ядро посчитано
  чужой линейкой» (`unconfigured_core_denominator`) — ровно то различие, ради
  которого статус вообще заведён. Во-вторых, **матчер путей собственный, не
  `fnmatch`**: `fnmatch` не знает про `/`, и на нём невыразимо правило «только
  корневые `*.py`» (`fnmatch("reviewer/x.py", "*.py")` истинно) — поэтому
  `_glob_to_regex` компилирует `**`/`*`/`?` вручную, с `**` пересекающим `/` и
  одиночным `*` — нет. В-третьих, **уникальность строки измерения — по
  `(repo, pr_number, COALESCE(task_key, ''))`**, а не по одному `task_key`:
  в SQL `NULL ≠ NULL`, и без `COALESCE` каждая задача без ключа получала бы
  собственную строку вместо одной переиспользуемой. Съём теперь трёхточечный —
  `publish_review` (с `run_id`), `finish_task` и CLI `reviewer measure-briefs`
  (оба без `run_id`) — общей точкой стал `reviewer.services.brief_quality.
  measure_and_record`; идентичность строки держится на тройке выше, а не на
  факте прогона ревью, поэтому более ранний съём без ревью и более поздний
  `publish_review` дописывают одну и ту же строку, а не плодят две.
- **Полная воронка находок в `review_findings` (outcome/reject_reason).** `review_findings` персистит **каждого кандидата**, а не только опубликованных: колонка `outcome` — терминальный исход одного из 6 состояний (`published_inline`/`published_summary`/`verify_rejected`/`gate_dropped`/`deduped`/`already_posted`), `reject_reason` — причина отсева (текст верификатора при `verify_rejected` через `VerdictIn.reason`; сработавшее правило политики через `ReviewPolicy.gate_reason` при `gate_dropped`; иначе `NULL`). Учёт — чистый юнит `reviewer/agent/outcomes.py::account_outcomes`, инвариант `len(rows) == len(candidates)` (сумма по 6 исходам = числу кандидатов). **`deduped`-разность считается по IDENTITY (`id()`), не по fingerprint**: точный дубль имеет тот же fingerprint, что выживший (`dedup_findings` возвращает те же объекты), поэтому fingerprint-diff недосчитал бы схлопнутые. `outcome` — новое поле-истина; старые `is_real`/`published`/`inline` заполняются как прежде (обратная совместимость). Миграция аддитивна/идемпотентна (`ADD COLUMN IF NOT EXISTS` + best-effort бэкфилл). При `status='error'` строки хранят намеченный `outcome`, но `published=False`.
- **MCP-сессия живёт в процессе сервера** между `prepare_review` и `publish_review` одного PR: `_Session(prepared, ctx)` в `MCPReviewService._sessions`. При повторном `prepare_review` для того же (repo, pr) сессия перезаписывается, старый VCS-провайдер закрывается (fail-soft).
- **Плагин** находится в `plugin/` в корне репозитория — это корень Claude Code-плагина для скилла `/rag-reviewer:review-pr`.
- **Общие reference-блоки промптов** вынесены в `plugin/skills/_common/` (единый источник: `findings-schema.md`, `anti-hallucination.md`, `tool-usage.md`, `branch-selection.md`). Скиллы и reference-промпты подключают их маркером `<!-- include: _common/<file>.md -->` (путь от `plugin/skills/`), который LLM-оркестратор разворачивает verbatim при сборке промпта субагента; скилл-специфичные части остаются в самих скиллах. Соответствие findings-schema ↔ `Finding` (`reviewer/vcs/base.py`) и корректность сборки промптов охраняют guard-тесты в `tests/skills/`.
- **Мульти-платформа VCS (GitHub + GitLab).** Тип провайдера — свойство репо, не PR. `reviewer index` определяет платформу из `git remote` (`derive_vcs_from_remote`) и пишет в таблицу `repo_vcs(repo→provider,base_url)`. При ревью (API-only движок) `_create_vcs_provider` читает `repo_vcs` ДО любого API-вызова и выбирает `GitHubProvider`/`GitLabProvider`; токен — из ENV по платформе (`GITHUB_TOKEN`/`GITLAB_TOKEN`, `GITLAB_URL` для self-hosted). Фолбэк при пустом `repo_vcs` — `VCS_PROVIDER` (дефолт github), что сохраняет обратную совместимость. Секретов в `.review.yml` нет (нет блока `vcs:`).
- **Закрытие задачи после PR (`finish_task`).** Скилл `/rag-reviewer:finish-task` после создания PR
  идемпотентно дописывает PR-ссылку и запрашивает generic done target через server-side MCP-тул
  `finish_task(..., board_type, target, provider_options)`. Провайдер возвращает отдельные
  `pr_link_added`, `done_set`, `already_closed` и warnings; общий слой затем делает write-through
  `fetch_one → normalize → index_task`.
  Связь двусторонняя: тот же слой fail-soft дописывает кликабельную ссылку на задачу
  (markdown, из `url_template`) в начало тела PR — маркер `<!-- reviewer:task-link -->`
  даёт идемпотентность, платформа резолвится по форме ссылки (`/pull/N` → GitHub,
  `/-/merge_requests/N` → GitLab), результат — в поле `task_link_status`
  (`added` | `already_present` | `failed`; `already_present` — идемпотентный no-op, ссылка уже была
  в теле PR, это норма, а не сбой), а legacy-`task_link_added` остаётся истинным только для `added`.
  Любая правка двигает last-modified, поэтому следующий
  `sync_board` сохраняет обновлённую задачу. Python пишет в доску и при болк-синке, и при
  `finish_task`; креды остаются в env. Добавлять type разрешено только полным путём: adapter →
  immutable spec → explicit registry line → full contract fixture → provider-specific tests →
  documentation matrix row. Partial registration запрещена.

- **Канал репорта багов самого reviewer (`report_bug`, PRI-239).** Скилл `report-bug` — тонкий
  триггер server-side lifecycle: триаж → анонимизация → сборка issue → публикация в
  `mimfort/rag_for_git`. Двухфазность — **серверный** инвариант, не договорённость с промптом:
  без `confirmed=True` тул возвращает `status="preview"` с полным итоговым текстом и ничего не
  отправляет, а при `non_interactive=True` подменяет публикацию фолбэком даже с `confirmed`
  («апрув» без человека апрувом не является). Фильтр «наш баг / не наш» живёт в
  `bugreport/triage.py` и решает по **форме** (класс симптома), а не по тексту: чужие проблемы
  (окружение, внешние сервисы, код пользователя, права, поведение LLM) канал не репортит — кроме
  `llm_behaviour` с `caused_by_skill_instruction=True`, потому что баг промпта тоже наш.
  Анонимизация (`bugreport/sanitize.py`) детерминирована и не доверена LLM; литералы установки
  (репо, ветки, хосты, секреты) вымарываются ДО эвристик, а пути внутри `reviewer/` и `plugin/`
  сознательно сохраняются — без них баг невоспроизводим. Порог шума: одно предложение на
  сигнатуру за жизнь процесса, отказ запоминается (`_bug_offered`/`_bug_declined`).
  Выключатели независимы: `REVIEW_BUG_REPORTS=false` (деплой) и `bug_reports: false` (`.review.yml`).
  **Автотриггер (PRI-240)** — `PostToolUse`-хук `plugin/hooks/reviewer_defect.py` на оба префикса
  имён reviewer-тулов. Он stdlib-only и self-contained (хуки запускает системный python3, где
  пакет `reviewer` не установлен), поэтому словарь классов симптомов продублирован и сверяется
  guard-тестом. Распознаёт ровно две формы: фрейм `reviewer/*` в traceback и `status` вне
  документированного набора тула (`DOCUMENTED_STATUSES`); неизвестный тул не судится вовсе.
  Нарушения инвариантов сознательно вне хука — в одном ответе их не видно. Allowlist чужих
  сигналов проверяется **первым** и перекрывает даже наш traceback. Две грабли зафиксированы
  тестами: голая подстрока `mcp` в allowlist глушила собственные трейсбеки (путь
  `reviewer/mcp/service.py`), а `json.dumps` ответа экранировал кавычки и фрейм `File "…"`
  переставал совпадать — поэтому текст собирается обходом строковых значений. Дедуп по сигнатуре
  живёт в файле в tempdir: каждый вызов хука — отдельный процесс, in-memory здесь бесполезен.
- **Расход ревью снимает клиентский хук, не сервер (PRI-247).** MCP-сервер видит только
  свои тул-вызовы, а не LLM-ходы оркестратора/субагентов — токены и стоимость ему взять неоткуда.
  Их снимает `PreToolUse`-хук плагина `plugin/hooks/review_cost.py` (stdlib-only, системный
  python3, без пакета `reviewer`): парсит транскрипт сессии Claude Code, взвешивает бакеты токенов
  (`fresh_in×1`, `output×5`, `cache_write×1.25`, `cache_read×0.1` — тариф из спайка PRI-246,
  условные единицы, не доллары) и пишет sidecar JSON по пути `sidecar_path(repo, pr)` =
  `tempfile.gettempdir()/reviewer-review-cost/<repo>-<pr>.json`. `publish_review` читает и удаляет
  sidecar через `reviewer/services/cost_sidecar.py::read_cost_sidecar`. Путь и формула веса
  **дублируются буквально** между хуком (`plugin/hooks/_transcript.py`), сервером
  (`cost_sidecar.py`/`reviewer/web/history.py`) и офлайн-эвалом
  (`eval/solve_task_metrics/cost.py`) — хук не может импортировать пакет `reviewer`, поэтому
  единого источника правды нет; совпадение пути и всех трёх словарей весов закреплено guard-тестом
  (`tests/hooks/test_review_cost.py`). Слияние с явными аргументами `model`/`usage`/`total_cost`
  публикации — **пофайловое**, не «всё или ничего»: `merge_metadata` берёт из sidecar только те
  поля, что явно не переданы, так что CLI, отдающий `model`, но не расход, не теряет sidecar-данные.
  Канал файловый и неработоспособен при удалённом MCP (хук и `reviewer-mcp` не делят файловую
  систему) — это штатный случай «sidecar отсутствует», ревью публикуется без метаданных расхода.
  Пошаговый трейс тул-вызовов (`review_steps`, стадии `analyze`/`verify`/`synthesize`/`client`) —
  отдельный, чисто серверный канал; веб-админка (`GET /api/runs/{id}/trace`) сливает оба канала в
  `by_stage` через объединение множеств стадий (`reviewer/web/history.py::merge_stage_costs`) —
  стадии хука (`orchestrator`/`risk`/`blast_radius`/...) и стадии трейса пересекаются, но не
  совпадают; у стадии без данных о расходе `cost` — `null`, не 0.
- **Секция `code` контекста задачи идёт мультизапросом, и её финальный ранкер — RRF, а не реранкер
  (PRI-255).** Подзапросы извлекаются детерминированно (`reviewer/mcp/subqueries.py`: пункты списков
  под заголовками «что сделать»/«критерии приёмки» + пул технических идентификаторов, cap
  `MAX_SUBQUERIES = 20`), эмбеддятся **одним** батчем (`VoyageEmbedder.embed_queries` поверх того же
  LRU), каждый гоняется через `store.hybrid_search`, выдачи сливаются `rrf_merge` в
  `reviewer/retrieval/multiquery.py::search_multi`. Неочевидно здесь три вещи. Во-первых,
  **реранка и cliff-отсечки в этом пути нет намеренно**: cliff считался по скорам реранкера против
  того же многотемного запроса, на размытом запросе все скоры низкие, отсечка падала до `floor=4` —
  это и был механизм, дававший измеренную медиану «2 файла», так что сохранить реранк значило бы
  сохранить сам дефект. Во-вторых, **текст каждого блока обрезается по границе строк**, потому что
  `as_context` режет рендер тупым `text[:8000]` и один чанк-класс на 400 строк выжигал весь
  символьный бюджет; `as_context` общий с ревью PR и потому не меняется — обрезка живёт в
  `multiquery.py::cap_block`. Модульная константа `MAX_BLOCK_CHARS` снята в PRI-256: её роль занял
  `CodeSectionLimits.chars_per_file` — доля на файл файлового бюджета секции, а не независимая
  четверть `max_tool_result_chars`. В-третьих,
  **`search_base` и публичный `search_codebase` не тронуты вовсе**: новый путь параллелен, зовёт то,
  что лежит НИЖЕ `search_base` (`store.hybrid_search`), а его хвост (`_is_test_path`,
  `_dedupe_overlapping`) переиспользует импортом; включён мультизапрос только внутри
  `prepare_task_context` (секции `code` и `test_exemplars`), секция `subsystems` по-прежнему
  получает единый запрос. Вырожденный вход (board-less, задача без списков) даёт набор `[q0]` и
  тождественен прежнему поведению. Замер приёмки — `eval/replay_report.md`, секция «Приёмка
  PRI-255»: медиана core-recall 0.225 → 0.3333, bulk 0.1548 → 0.1825 на одном `indexed_sha`; цена —
  precision 0.875 → 0.5 при росте числа файлов с 2 до 4, что и было целью.
  Константа RRF объявлена ровно один раз (PRI-267): `RRF_K` в `reviewer/rrf.py`, а оба SQL
  (`index/store.py::hybrid_search`, `tasks/store.py::search`) берут значение именованным
  параметром `%(rrf_k)s::int` — числового литерала в знаменателе нет вовсе, поэтому для этих
  двух мест расхождение не обнаруживается, а невозможно (предел — гипотетический новый SQL со
  своим литералом мимо `reviewer.rrf`: guard смотрит на эти два call-сайта и `multiquery`, а не
  на весь репозиторий, и такого не поймает). Неочевидны три вещи. Модуль лежит в **корне
  пакета**, а не рядом с `rrf_merge`: оба store ниже `retrieval` (`multiquery` уже импортирует
  `index.refs`), и импорт в обратную сторону развернул бы направление зависимости. Каст
  `::int` не косметика: без него тип параметра выводится из контекста сложения и может
  разойтись между simple- и prepared-протоколом psycopg. Guard
  `tests/test_rrf_k_single_source.py` смотрит на **фактически переданные драйверу** sql и
  params, а не на текст исходника, и требует ровно двух плейсхолдеров на каждый store —
  подставленная одна ветка CTE из двух разъехалась бы так же молча, как прежний литерал.
- **Бюджет секции `code` файловый, и символьный потолок у него производный (PRI-256).**
  До PRI-256 выдачу секции резала арифметика, а не ранжирование: `search_multi` отбирал
  `ceiling = 15` чанков, `cap_block` жал каждый блок до 2000 символов, а `ContextPack.as_context`
  срезал весь текст на `settings.max_tool_result_chars = 8000` — то есть до сборщика брифа
  физически доезжали 8000 ÷ 2000 = **4 блока**, остальные 11 отбрасывались молча. Отсюда и
  измеренная медиана «4 файла». Теперь единица бюджета — файл: `CodeSectionLimits`
  (`reviewer/policy/context_limits.py`, ключ `.review.yml` — `context_limits.code_section`) задаёт
  `max_files`, `max_chunks_per_file`, `chars_per_file` (изначальные дефолты PRI-256 — `12`/`1`/`1300`;
  текущие, после PRI-259, — `20`/`1`/`975`, см. абзац ниже), а `diversify_by_file`
  (`reviewer/retrieval/multiquery.py`) применяет их между `_dedupe_overlapping` и рендером.
  Четыре вещи здесь неочевидны. Во-первых, **операционный бюджет секции** —
  `max_files × max_chunks_per_file × chars_per_file` — обеспечивает `cap_block`, режущий
  ИСХОДНЫЙ текст блока ДО рендера, и именно он делает объём линейным по числу файлов; отдельным
  ключом он не выведен — третьего регулятора, который мог бы рассинхронизироваться с двумя
  остальными, просто нет. Во-вторых, свойство `CodeSectionLimits.max_chars`
  (`max_files × max_chunks_per_file × chars_per_file × 3 // 2`) — это **не** операционный
  бюджет, а страховочный
  потолок ПОСЛЕ рендера: полуторный запас покрывает накладные рендера (префиксы номеров строк
  ~10 %, заголовки блоков ~4 %) и случай, когда `cap_block` удерживает первую строку целиком,
  будучи она длиннее лимита. В-третьих, **порядок обязателен**: диверсификация идёт строго ПОСЛЕ
  `_dedupe_overlapping`, который оставляет самый широкий чанк из вложенных; обратный порядок
  удержал бы вложенный метод и выбросил охватывающий класс. В-четвёртых, бюджет **отдельный от
  `CodebaseLimits`**: тот обслуживает публичный `search_codebase`, `/ask` и грунтовку, где единица
  бюджета — чанк; `search_base` и публичный тул этой задачей не затронуты вовсе. Прежняя чанковая
  отсечка `[:lim.ceiling]` в `search_multi` снята, но `lim.ceiling` остался числом сидов
  graph-expansion — это глубина расширения, а не бюджет выдачи. Цена, которую стоит знать:
  глобальный `settings.max_tool_result_chars` эту секцию больше **не** ограничивает (раньше
  ограничивал — им и была стена), верхнего предохранителя над `code_section` нет, поэтому
  `max_files: 200` в `.review.yml` даст тул-результат под 400 тыс. символов; это симметрично
  остальным секциям лимитов (у `search_codebase.ceiling` верхней границы тоже нет — политика
  доверяет оператору). Лимиты действуют на обе секции, идущие через `_search_codebase_multi`:
  `code` и `test_exemplars`. Замер приёмки — `eval/replay_report.md`, раздел «Приёмка PRI-256».
- **Дефолт бюджета секции `code` покупает ширину ростом бюджета на 25 %, глубину держит на полу
  (PRI-259).** Свод рычагов секции
  (мультизапрос PRI-255, файловый бюджет PRI-256, подмешивание similar-diffs PRI-257) не брал
  порог приёмки «медиана bulk core-recall ≥ 0.55» — метрика упиралась не в ранжирование, а в
  арифметику: у всех bulk-задач `предсказано` равнялось ровно `max_files`, бюджет выгорал
  полностью. Решение — обменять ширину бюджета на глубину фрагмента, а не расширить бюджет:
  дефолт стал `max_files=20`, `chars_per_file=975` (было `12`/`1300`), операционное произведение
  `max_files × max_chunks_per_file × chars_per_file` при этом выросло с 15 600 до 19 500 (+25 %)
  — сознательный, а не побочный размен. Причина именно такой формы: core-recall считает пути, а
  не тела символов, поэтому расширение ширины напрямую покупает recall и безопасно (лишние слоты
  никого не вытесняют), а сокращение глубины — нет: полезность файла «для чего он нужен» лежит в
  его содержимом, и деградацию объяснений в брифе метрика не ловит никаким числом. Отсюда пол
  `chars_per_file ≥ 975` (сигнатура символа плюс несколько строк тела) — величина, взятая не из
  метрики. Справочный замер `20 × 780` (ниже пола, бюджет не превышает исходные 15 600) дал
  числа, тождественные `20 × 975`: это прямое эмпирическое подтверждение слепоты метрики к
  глубине, а не повод опустить пол. Замер приёмки — `eval/replay_report.md`, раздел
  «Приёмка PRI-259».
- **Секция `code` подмешивает фактические diff-пути похожих задач, и квота под них — резерв,
  а не потолок на остаток (PRI-257).** Источник один — `similar-diffs`: пути реальных диффов
  похожих задач, `reviewer/retrieval/augment.py::collect_similar_task_paths` берёт их из таблицы
  `brief_quality` (точнее — пути уже классифицированы как core) и, если там пусто, из git-фолбэка
  `gitutil.paths_touched_by_grep` по ключу задачи и его алиасам (сообщения коммитов,
  merge-коммиты веток `feat/PRI-N-...`). `test_exemplars` этого подмешивания не получает — только
  секция `code`. `CodeSectionLimits.max_augmented_files` (ключ `.review.yml` —
  `context_limits.code_section.max_augmented_files`, дефолт 3) — файловый РЕЗЕРВ внутри
  `max_files`, не потолок на остаток: `search_multi` сначала собирает полную гибридную выдачу
  (`hybrid_final`) на весь бюджет `max_files`, как если бы подмешивания не было, и лишь потом
  считает известность augmented-кандидатов по НЕЙ — не по сырому пулу ретрива (`merged` +
  graph-expansion, десятки путей). Если считать по сырому пулу, рычаг теряет ровно тот файл,
  который гибрид нашёл, но ранжировал слишком низко для попадания в `max_files`, — а это и есть
  основной случай, ради которого рычаг существует. Резерв — фактический (`min(len(augmented),
  max_files)`), не номинальная квота: без кандидатов гибрид получает бюджет целиком, без потери
  слотов на пустом сигнале. Квота считает ПОДМЕШАННЫЕ файлы, а не рассмотренных кандидатов:
  списки путей часто открываются непроиндексированными файлами (`docs/*`, `README.md`,
  `CLAUDE.md`, `*.jsonl` — чанки есть только у `.py`), поэтому `_augment_items` сначала собирает
  всех кандидатов (до предохранителя `AUGMENT_FETCH_LIMIT = 40` — это потолок размера ОДНОГО
  запроса `fetch_retrieved_at_paths`, а не бюджет выдачи), одним запросом узнаёт, у кого есть
  чанки, и только из реально найденных берёт первые `max_augmented_files`. Нота видимости
  (`— подмешано N файлов: similar-diffs (квота Q)`) — единственный способ увидеть в тексте
  секции, сколько файлов пришло из подмешивания, а не из гибрида; без неё разница неотличима на
  глаз. Все три механизма (потолок-на-остаток, известность по сырому пулу, квота по кандидатам, а
  не по найденному) по отдельности обнуляли эффект рычага до нуля на живом замере — история
  трёх последовательных фиксов и итоговый разбор в
  `.superpowers/sdd/2026-08-17-pri-257-augmented-candidates/step8-measurement.md`. Итоговый замер
  (42 задачи, один `indexed_sha`, сторона «до» — уже улучшенный PRI-255/256 путь `multiquery`):
  медиана core-recall 0.5 → 0.75, precision 0.167 → 0.333, 28 попаданий в ядро на 35 подмешанных
  путей, ни одной задачи с падением recall — `eval/replay_report.md`, раздел «Приёмка PRI-257». На
  момент замера таблица `brief_quality` была пуста, поэтому весь измеренный эффект дал git-фолбэк;
  с накоплением истории прогонов табличный источник добавится к измеренному, а не заменит его.
  Второй рычаг — **git-со-изменяемость (co-change) была реализована, измерена и снята**: те же 42
  задачи дали 4 попадания в ядро на 34 подмешанных пути (12 % точности), просадку bulk
  (0.3730 → 0.3544), одну вытесненную из выдачи core-задачу и ноль измеримого вклада поверх
  similar-diffs (вариант «оба» численно совпал с одним similar-diffs). Со-изменяемость ловит
  спутники правки (тесты, соседние модули), а не ядро задачи — реализацию воспроизводить не
  стоит, отрицательный результат уже задокументирован в `eval/replay_report.md`.
- **Состав кластеров сводок подсистем как источник подмешивания опробован и снят (PRI-258).**
  Секция `subsystems` контекста задачи отбирает релевантные кластеры сводок, но их состав
  (`member_node_ids`) в секцию `code` не попадал — подсистема названа, файлы нет. Рычаг разворачивал
  топ-3 кластера в пути-кандидаты со своим файловым резервом и был снят по замеру: 2 попадания в
  ядро на 92 подмешанных пути (2 % — вдвое хуже снятого co-change), вытеснено 9 core-хитов, которые
  гибрид находил сам, медиана core-recall 0.75 → 0.6667, bulk 0.3889 → 0.3333, 7 задач упали против
  одной выросшей (43 задачи, один `indexed_sha`, раздел «Приёмка PRI-258» в `eval/replay_report.md`).
  Неочевидно здесь одно: **ноль дала природа сигнала, а не механика бюджета** — по прецеденту
  PRI-257 это проверялось ОТДЕЛЬНО до вердикта, и нота видимости подтвердила, что 5 из 6 задач
  выбирают квоту разворота целиком, то есть файлы доезжали до секции. Кластер описывает подсистему
  целиком, а ядро правки — несколько файлов внутри неё; попадание в подсистему не есть попадание в
  ядро. От рычага в коде остались две вещи, обе полезные сами по себе: подмешивание принимает
  **список именованных источников** (`AugmentSource(name, paths, quota)`, каждый со своей квотой и
  накопительной известностью — в проде список из одного элемента, `similar-diffs`) и **приоритет
  подмешанных кандидатов по RRF-рангу сырого пула** (`rank_by_path` в `multiquery.py`): путь,
  который гибрид нашёл, но не поднял до `max_files`, идёт раньше пути, которого гибрид не нашёл
  вовсе. Ранг считается по сырому пулу ДО фильтра тестов и дедупа — по финальной выдаче считать
  нельзя, это тот же класс дефекта, что трижды обнулял рычаг в PRI-257.
- **Недоступность хранилища — классифицированный сигнал, и решает её ТИП исключения (PRI-268).**
  `reviewer/storage_health.py` — единственный источник правды: `is_storage_unavailable` решает
  `isinstance`'ом, а не по тексту, потому что в тексте `psycopg.OperationalError` живёт DSN с
  паролем (тот же мотив, что у `config/fetch_errors.py`, PRI-234). Покрытие подобрано по смыслу
  «лечится подъёмом контейнеров»: `psycopg.OperationalError` — да (а вместе с ним и
  `psycopg_pool.PoolTimeout`, он подкласс), neo4j `ServiceUnavailable`/`SessionExpired` — да, а
  `psycopg.ProgrammingError` и neo4j `AuthError` — намеренно нет: ошибку схемы и неверные креды
  `reviewer start` не чинит, и маскировать их под «инфраструктура лежит» значило бы врать.
  Три вещи здесь неочевидны. Во-первых, **замыкание существует ради времени ответа, а не ради
  чистоты**: без него каждая из девяти секций `build_task_context` платит собственный 30-секундный
  таймаут пула, и «секунды» из критерия задачи недостижимы даже при исправленном `index_batch`;
  поэтому первая же недоступность взводит флаг на один вызов, а остальные секции получают свой
  default и запись о пропуске, не трогая источник. Во-вторых, **`gap()` теперь всегда 5-ключевой**
  (`section`, `reason`, `cause`,
  `cause_detail`, `remedy`): потребитель обязан ветвиться по `cause`
  (`storage_unavailable` | `unknown`), а не по прозе `reason` — проза меняется, класс нет; `remedy`
  равен `null`, когда эндпоинты не loopback (удалённым хранилищам `reviewer start` не поможет).
  В-третьих, **сервер контейнеров не поднимает никогда** — это решение пользователя, а не
  умолчание: MCP только называет лекарство, а шаг `0a.` скилла `solve-task` спрашивает человека.
  Отдельно стоит знать про измерение: тест, проверяющий, что при лежащем хранилище не тратится
  квота Voyage, легко написать зелёным по построению — фикстура, у которой `existing_hash` падает
  на ЛЮБОМ ключе, оставляет `to_embed` пустым структурно, и guard `and not storage_down` не влияет
  ни на что. Дыру нашли мутационно (снять guard с копии модуля вне рабочего дерева — тест обязан
  покраснеть); постоянный тест теперь использует смешанный стор, где первая задача доходит до
  `to_embed`, а падает вторая.
  Пятый ключ `cause_detail` добавлен в PRI-277 и уточняет причину ВНУТРИ класса
  `storage_unavailable`: `auth_failed` | `missing_database` | `null`. Сам `cause`
  при этом не меняется намеренно — шаг 0a скилла `solve-task` ищет равенство
  `storage_unavailable`, и уточнение самого `cause` тихо перестало бы показывать
  пользователю auth-сбой вовсе. Решение принимает `classify_storage_failure`
  (`reviewer/storage_health.py`) — единственный источник и класса, и уместности
  совета `reviewer start`, общий для MCP и `reviewer check`. Ограничение, которое
  надо знать: сообщения libpq локализуются по `lc_messages` сервера, поэтому на
  не-английской локали паттерны не совпадут и случай уедет в нераспознанную ветку
  с вымаранным отрывком — деградация безопасная, но различение не гарантировано.
  **Успешные строки `reviewer check` печатают эндпоинт через `mask_endpoint`, а
  ветка `✗ Postgres: {err}` — сырой текст драйвера, и асимметрия намеренная.**
  `mask_endpoint` знает пароль точно (это САМ эндпоинт) и потому убирает только
  его, оставляя хост, порт, пользователя и базу: без них строка не отвечает на
  вопрос «куда я подключаюсь». В ветке отказа текст чужой — драйверный, — там
  вымарывание идёт по литералам эндпоинта (`_redact`) и только на границе MCP;
  в терминале самого оператора сообщение остаётся полным намеренно, потому что
  срезанный хост и порт — это ровно та диагностика, за которой `check` и зовут.
  **Замыкание раздельно по хранилищам, а отказ графа приходит из preflight полем
  (PRI-276).** `_StorageState.down` — множество бэкендов (`storage_health.BACKEND_GRAPH`
  / `BACKEND_POSTGRES`), а вердикты — словарь по ним: один флаг на двоих при мёртвом
  Neo4j отменял бы и поиск по коду, теряя не одну секцию, а все. Какой бэкенд упал,
  решает тип исключения (`storage_backend`), а параметр `backend` у `_safe` отвечает
  на другой вопрос — кого пропускать; дефолт у него Postgres, и явная разметка есть
  ровно у `related.linked`. Три вещи здесь неочевидны. Во-первых, **нота
  `(task graph unavailable)` живёт на границе MCP-тула, а не в `TaskService`**: пока
  её ставил сервис, исключение не доходило до классификатора вовсе — это и был
  дефект (пустой `gaps` при обеднённом контексте), а публичный контракт тула при
  этом обязан остаться строкой. Во-вторых, **preflight сообщает об отказе графа
  полем `BranchStatus.graph_error`, а не броском**: бросок потерял бы секцию целиком
  (`graph_nodes=None` — валидная деградация, на ней стоит CLI `status`), а без
  сигнала оттуда `related.linked` платит второй таймаут; `build_task_context`
  извлекает ключ `graph_error` из словаря preflight и наружу его не отдаёт — payload
  читает LLM. В-третьих, **при мёртвом Postgres `related.linked` теперь вызывается**:
  граф — другое хранилище, и при живом Neo4j секция собирается вместо того, чтобы
  теряться зря; ценой одного лишнего захода, когда мертвы оба. Цену самого захода
  снимают явные таймауты драйвера (`GraphStore.__init__`, ключи `Settings`
  `neo4j_connection_timeout`/`neo4j_acquisition_timeout`/`neo4j_max_retry_time`):
  дефолты neo4j — 30/60/30 с, и основную часть наблюдавшихся 162 с давали именно
  ретраи транзакции.
  **Замыкание ловит только то, что до него доходит: fail-soft выше по стеку крал у него
  таймаут (PRI-275).** `_TaskContextDeps._clone_path` читает таблицу `repo_clone` ДО
  `build_status_report` — и `_repo_clone_path` гасил там исключение сам, поэтому
  `_StorageState.down` не взводился, а `get_index_meta_row` уходил во второй таймаут пула:
  ответ на остановленных хранилищах стоил 66.55 с вместо 37.05 с (`eval/pri275_measurement.md`).
  Лечится keyword-only `strict` у `_repo_clone_path`: при `strict=True` недоступность
  хранилища пробрасывается вызывающему. Три вещи здесь неочевидны. Во-первых, **строгость
  включена ровно в одной точке** — в `_clone_path` секции preflight, исполняемой внутри
  `_safe`; два других потребителя (`_resolve_policy`, учёт качества брифа) остаются
  fail-soft, потому что обработать проброс им нечем и их сообщения об ошибке точнее общего
  «policy could not be resolved». Во-вторых, **тесты считают заходы в стор, а не время**:
  на фейках обращение к `repo_clone` бесплатно, что и скрыло дефект от unit-уровня на
  протяжении всего PRI-268. В-третьих, тип исключения в тестах — `psycopg_pool.PoolTimeout`,
  а не голый `OperationalError`: это машинный контракт с PRI-274. Контракт сработал ровно так,
  как задумывался, но исход оказался обратным ожидаемому: PRI-274 **оставила** PoolTimeout
  внутри `is_storage_unavailable` (вывод лишил бы замыкание возможности поймать его на первом
  же сбое) и развела случаи через `cause_detail`. Тест на этом покраснел и заставил согласовать
  ожидание — см. абзац про `cause_detail: pool_exhausted` ниже. Ожидать после фикса ровно 30 с
  нельзя: `related.linked` — другое хранилище и платит свой заход по таймаутам драйвера.
- **Исчерпанный пул — деталь внутри класса, а мёртвый эмбеддер — свой класс (PRI-274 + PRI-272).**
  Обе задачи отвечают на один вопрос («какой класс причины получает сбой») и потому сделаны одной
  веткой. `PoolTimeout` **остаётся** внутри `is_storage_unavailable`: вывести его значило бы лишить
  `_StorageState` возможности замкнуть его на первом же сбое и вернуть восемь таймаутов по 30 с
  вместо одного — прямая регрессия PRI-268/275/276. Отличимость даёт `cause_detail:
  "pool_exhausted"` с `remedy: null`, и решается она `isinstance` **до** текстовых паттернов
  (тип конкретнее текста — то же основание, по которому `auth_failed` проверяется раньше
  `missing_database`), **но одного типа мало**: прод ходит в Postgres только через пул
  (`ChunkStore._connect`), а пул с `open=False` не пробрасывает отказ соединения наружу — фоновые
  воркеры молча ретраятся, и остановленный контейнер приходит тем же `PoolTimeout`, что и реально
  занятый пул (замерено на закрытом порту: 30 с и `PoolTimeout`, не `OperationalError`). Поэтому
  тип дополнен НАБЛЮДЕНИЕМ: одноразовая проба `psycopg.connect(dsn, connect_timeout=2)` мимо пула.
  Соединилась — пул занят по-настоящему; упала — классифицируется исключение ПРОБЫ, и лежачий
  контейнер получает обратно свой `reviewer start`, а протухший пароль — `auth_failed`. Пустой
  вердикт пробы не отбирает лекарство: «не знаю причину» ≠ «лечить нечем». Проба инъектируется
  keyword-only параметром `probe` (в проде не передаётся — сигнатура для вызывающих не менялась),
  иначе unit-тест открывал бы сокет. Две секунды платятся один раз за вызов `build_task_context`
  (дальше держит замыкание) и только поверх уже потраченных пулом тридцати. Эмбеддер, наоборот,
  получает равноправный `cause:
  "embedder_unavailable"`: уточнять им `storage_unavailable` было бы ложью — Voyage не хранилище,
  контейнеры подняты, `reviewer start` не лечит. Класс решается типом (`voyageai.error.VoyageError`
  и наследники) **за вычетом** `RateLimitError`: троттлинг на free tier — штатное состояние,
  его уже отрабатывает `with_voyage_retry`, и звать это недоступностью значило бы поднимать
  тревогу на каждом втором прогоне. Рядом с ним вычитаются ошибки самого запроса —
  `InvalidRequestError` (400, клиент бросает её и локально, без сети), `MalformedRequestError`
  (422) и `VideoProcessingError`: «запрос неверен» — не «сервис лежит», а цена ошибки здесь
  несимметрична, одна такая внутри `warm_board` снимает четыре секции контекста при живом
  эмбеддере. Иерархия `voyageai.error` плоская — `RateLimitError` не
  наследник `APIError`, поэтому вычитание выражается вторым `isinstance` по списку имён, а не
  порядком веток; имена резолвятся лениво, и версия клиента без какого-то класса оставляет
  предикат рабочим.
  Пять вещей здесь неочевидны. Во-первых, **`_StorageState` замыкает по ИСТОЧНИКАМ, а не по
  хранилищам**: к `postgres`/`graph` добавлен `embedder`, и состояние хранит класс причины по
  источнику — мёртвый Voyage не отменяет секции, которым нужен только Postgres, по тому же
  доводу, что и в PRI-276. Во-вторых, **барьеров на пути контекста оказалось три, а не два**:
  спека называла `TaskService.search_hits` и `MCPReviewService._search_codebase_multi`, но ниже
  них лежит `_embed_pairs` (`retrieval/multiquery.py`), гасивший и батч, и одиночный откат и
  возвращавший `[]`, — с ним `strict` для секций `code`/`test_exemplars` не мог сработать в
  проде ни разу, хотя обе половины по отдельности были покрыты и зелены. Проброс во внешнем
  `except` идёт **до** одиночного отката: откат существует ради исчерпанной квоты, а
  `RateLimitError` недоступностью не считается, так что эта деградация продолжает работать.
  `ValueError` от `zip(..., strict=True)` при этом остаётся fail-soft — это баг кода, а не отказ
  сервиса. В-третьих, **секция `task` лечится структурным признаком, а не пробросом**: `deps.task`
  не бросает — задачи действительно нет в сторе, потому что её туда не пустил упавший `warm_board`,
  — поэтому `index_batch` кладёт в каждую строку результата машиночитаемое `failure`
  (`"embedder"` | `"storage"` | `None`), свод синка получает булев `embedder_failed`, и
  `build_task_context` читает именно его. Разбирать строки `warnings` запрещено: их текст —
  формат клиента Voyage, он меняется с версией. В-четвёртых, **`failure: "storage"` гейтится тем
  же предикатом, что `storage_down`**: без гейта `psycopg.ProgrammingError` (настоящий баг SQL,
  намеренно исключённый из `is_storage_unavailable`) уехал бы в класс «лечится подъёмом
  контейнеров». В-пятых, **клиент ветвится по классу, а не по значению**: шаг 0a
  `solve-task/references/preflight.md` искал равенство `storage_unavailable` и пропустил бы новый
  класс молча; guard-тест `tests/skills/test_source_cause_vocabulary.py` сверяет набор классов в
  тексте скилла с набором в `storage_health`. Его мутационная проверка требует править **оба**
  файла (`SKILL.md` и `preflight.md`): имена классов живут в обоих, и снятие условия только в
  одном оставляет тест зелёным — ровно та ловушка, ради которой мутационная проверка и делается.

## Соглашения

- Внешние сервисы (GitHub, Voyage, Postgres, Neo4j) изолированы за интерфейсами и мокаются в unit-тестах; реальные вызовы — только в integration/E2E.
- Коммиты: **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude). Стиль сообщений — Conventional Commits на русском (`feat(agent): …`, `fix(index): …`).

## Грунтовка reviewer в фазах план/ревью (опционально)

Догфуд PRI-203. В фазах планирования/ревью, если reviewer-MCP подключён и его base-индекс
свеж (`reviewer status --json` -> `drift == 0`), предпочитай session-less тулы reviewer
голому grep для кросс-файловых фактов: `search_codebase` (релевантный код), `callers`
(blast-radius сигнатуры, которую собираешься менять), `related_symbols`, `definition`,
`implementations`, `family` (кто ещё такой же — наследники/сиблинги/структурные реализации
контракта). Точечно — пропускай мелкие/знакомые правки и файлы, уже в контексте (Voyage 3 RPM / 10K TPM).
Base-индекс отслеживает целевую ветку, не рабочее дерево: грунтовка надёжна для существующего
кода, но слепа к символам, только что правленным локально — их проверяй через Read. Если
reviewer недоступен или индекс устарел — откат в grep/Read.
