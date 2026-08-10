# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## О проекте

`rag_for_git` — агент автоматического ревью pull/merge request'ов: **RAG (гибридный поиск) + граф кода + Claude Code-плагин**. На вход — PR на GitHub, на выход — inline-комментарии на строки диффа + сводка. Целевой язык анализа — Python; VCS — GitHub (за интерфейсом `VCSProvider`). Подробный разбор архитектуры и потока данных — в `README.md` (написан на русском, сверен с кодом).

Язык проекта — **русский**: комментарии, докстринги, сообщения CLI. Сохраняй этот стиль в новом коде.

## Команды

```bash
# Установка (Python 3.11–3.13)
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

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

# Точечные прогоны
.venv/bin/pytest tests/index/test_store_hybrid.py     # один файл
.venv/bin/pytest tests/policy/test_policy.py::test_name -q   # один тест

# Линт
.venv/bin/ruff check .        # line-length 100, target py311
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
| `reviewer/tools/` | инструменты MCP-агента PR-сессии (`search_code`, `get_related_symbols`, `read_file`, `get_definition`, `find_callers`, `get_changed_file_diff`); session-less варианты для Q&A — `search_codebase`/`related_symbols`/`callers`/`definition`/`implementations` в `mcp/service.py` |
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
  - **tree-sitter** (`graph/builder.py`) — быстрый fallback без внешних зависимостей, только CALLS по имени.
  - Режим `auto` (по умолчанию): если `scip-python` найден в PATH — SCIP, иначе tree-sitter. При `backend=scip` сбой пробрасывается; при `auto` — откат на tree-sitter с `log.warning`. Команда `reviewer index` полностью перестраивает граф (clear + upsert), чтобы рёбра разных бэкендов не смешивались.
- **Voyage free tier = 3 RPM / 10K TPM.** TPM — главный блокер: полный `reviewer index` большого репо упирается в лимит и троттлится; есть retry/backoff (`index/_retry.py`). Ревью одного PR (overlay + query-эмбеддинги) в лимит укладывается.
- **`.review.yml` берётся из целевой (base) ветки**, не из PR — PR не может ослабить собственное ревью.
- **Реестр досок и `task_board`.** `BoardProviderRegistry` — единственная точка расширения: каждый зарегистрированный immutable `BoardProviderSpec` описывает factory, credential schema, setup/validation metadata и runtime options. Generic MCP, `SyncService`, `Settings` и installer не ветвятся по provider type. `.review.yml` целевой ветки выбирает зарегистрированный `task_board.type`, `project`, generic `create_target`/`done_target` и non-secret `options`; пустой `task_board:` выключает доску для репо. Новая форма имеет приоритет над legacy mapping (`done_column`/`done_state` → `done_target`, `status_field` → `options.status_field`) в течение одного compatibility-релиза. Креды приходят только из server-side env через `ProviderCredentialSource`; `board_config()` и MCP ошибки их не возвращают. См. `docs/board-providers.md`.
- **Одиннадцать зарегистрированных типов досок** (PRI-217): `yougile`, `youtrack`, `jira`, `github`, `trello`, `linear`, `clickup`, `asana`, `yandex_tracker`, `kaiten`, `weeek`. Новые адаптеры строятся на общем транспорте — `boards/restbase.py` (`RestBoardBase`: lifecycle клиента, вымарывание секретов, разделение read/write), `boards/pagination.py` (offset / page / cursor / `Link`-заголовок), `boards/graphql.py` (`GraphQLClient` для Linear), `boards/yfm.py` (YFM → markdown для Yandex Tracker); retry, `Retry-After` и категоризация статусов остаются в `BoardHttpClient`, провайдер добавляет лишь `rate_limit_hint`. **Долг:** три исходных адаптера (`yougile`, `youtrack`, `jira`) старше этого слоя и держат свою httpx-обвязку — ретрофит сознательно вне скоупа. Ключи: нативные у `linear`/`yandex_tracker`, у остальных синтезируются из option `key_prefix` (нативный id — в `RawTask.board_id`/`aliases`). OAuth loopback не поддерживается нигде (плагин работает в headless-CLI и по SSH) — только PAT/API-ключ и `help_url`.
- **Болк-синк задач — server-side ETL, не LLM (`sync_board`).** Скил `sync-tasks` — тонкий триггер generic lifecycle: `sync_board(..., board_type, provider_options)` резолвит зарегистрированный тип, проверяет безопасную credential schema, создаёт provider с immutable options и гарантированно вызывает `close()`. `reviewer-mcp` перечисляет доску по REST за полным `TaskBoardProvider` контрактом, нормализует в `TaskBrief` и индексирует через `TaskService.index_batch`; LLM не перечисляет доску и не передаёт текст задач → O(1) токенов. Watermark ключуется `ref="tasks:<type>:<board>"`; повторный синк трогает ~0 задач, а `--limit` отключает purge и продвижение курсора. Задачи глобальны (таблица `tasks` и граф `:Task` без repo-скоупа), но синк и выдача скоупятся `task_board.project`; клиент передаёт этот project, сервер repo-агностичен.
- **SHA base-индекса** хранится в таблице `index_meta` (пишется при `reviewer index`). При каждом `prepare_review` (MCP) SHA сравнивается с `base_sha` PR и при расхождении автоматически досинхронизируются чанки изменившихся файлов через GitHub compare API. Граф (Neo4j) **также** инкрементально досинхронизируется в этом шаге (tree-sitter, repo-scoped, входящие `CALLS`-рёбра сохраняются, fail-soft). Полная точность (рёбра `IMPLEMENTS` + все `CALLS`) восстанавливается ручным `reviewer index` с SCIP.
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
  печатает эффективные значения и блок `skipped`, но код возврата остаётся `1`.
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
  виден в отчёте (`committed: local|vcs`, ключ `committed_source` в JSON); путь к клону в
  диагностику не попадает.
- **Мульти-репо через `repo`-дискриминатор**: один деплой обслуживает N репозиториев через `repo` (`owner/name`) в Postgres (`chunks`/`index_meta`) и Neo4j (`:Symbol.repo`, составная уникальность `(repo, branch, id)`). Индексация: `reviewer index --repo owner/name` (или derive из git remote `origin` / `DEFAULT_REPO`). Граф задач `:Task` глобален — задача может покрывать несколько микросервисных репозиториев. Каждое ревью изолировано в рамках своего репо (без кросс-репо ретрива).
- **`reviewer check`** проверяет готовность окружения (ключи, Postgres, Neo4j, GitHub) без трат квот Voyage.
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
- **Наблюдаемость (`reviewer/web/`)**: каждый `publish_review` пишет в Postgres итоги прогона (`review_runs`/`review_findings`, гейт `REVIEW_HISTORY`) — fail-soft. Веб-админка (FastAPI `reviewer serve`) читает **ту же** БД.
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
  `/-/merge_requests/N` → GitLab), результат — в поле `task_link_added`. Любая правка двигает last-modified, поэтому следующий
  `sync_board` сохраняет обновлённую задачу. Python пишет в доску и при болк-синке, и при
  `finish_task`; креды остаются в env. Добавлять type разрешено только полным путём: adapter →
  immutable spec → explicit registry line → full contract fixture → provider-specific tests →
  documentation matrix row. Partial registration запрещена.

## Соглашения

- Внешние сервисы (GitHub, Voyage, Postgres, Neo4j) изолированы за интерфейсами и мокаются в unit-тестах; реальные вызовы — только в integration/E2E.
- Коммиты: **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude). Стиль сообщений — Conventional Commits на русском (`feat(agent): …`, `fix(index): …`).

## Грунтовка reviewer в фазах план/ревью (опционально)

Догфуд PRI-203. В фазах планирования/ревью, если reviewer-MCP подключён и его base-индекс
свеж (`reviewer status --json` -> `drift == 0`), предпочитай session-less тулы reviewer
голому grep для кросс-файловых фактов: `search_codebase` (релевантный код), `callers`
(blast-radius сигнатуры, которую собираешься менять), `related_symbols`, `definition`,
`implementations`. Точечно — пропускай мелкие/знакомые правки и файлы, уже в контексте (Voyage 3 RPM / 10K TPM).
Base-индекс отслеживает целевую ветку, не рабочее дерево: грунтовка надёжна для существующего
кода, но слепа к символам, только что правленным локально — их проверяй через Read. Если
reviewer недоступен или индекс устарел — откат в grep/Read.
