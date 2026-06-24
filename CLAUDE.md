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
.venv/bin/pytest -q                                   # unit: быстрые, на фейках, внешние API не дёргают
.venv/bin/pytest -m integration                       # integration: нужны поднятые Postgres/Neo4j + ключ Voyage
.venv/bin/pytest tests/index/test_store_hybrid.py     # один файл
.venv/bin/pytest tests/policy/test_policy.py::test_name -q   # один тест

# Линт
.venv/bin/ruff check .        # line-length 100, target py311

# CLI (после pip install -e .)
reviewer check                                    # проверить готовность окружения (ключи, Postgres, Neo4j, GitHub)
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
# 2. Использовать скилл /rag-reviewer:reviewer_review-pr (из plugin/)
#    Плагин вызывает prepare_review → analyze (Claude subagents) → publish_review

# На хосте (для разработки фронта): pip install -e ".[web]" && (cd web/frontend && npm install && npm run build) && reviewer serve
```

`pytest` по умолчанию **исключает** integration-тесты (`addopts = -m 'not integration'` в `pyproject.toml`) — маркер `integration` помечает тесты, требующие поднятых Postgres/Neo4j.

## Архитектура

Ядро — библиотека `reviewer/`, собираемая в `reviewer/app.py::build_components(settings)` из `Settings` (pydantic-settings, `.env`). Точка входа — `reviewer/entrypoints/cli.py` (Click) и `reviewer/entrypoints/mcp_server.py` (FastMCP).

Поток ревью: **prepare_review** (MCP) → **analyze** (Claude subagents через скилл `/rag-reviewer:reviewer_review-pr`) → **publish_review** (MCP).

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
| `reviewer/tools/` | инструменты MCP-агента PR-сессии (`search_code`, `get_related_symbols`, `read_file`, `get_definition`, `find_callers`, `get_changed_file_diff`); session-less варианты для Q&A — `search_codebase`/`related_symbols`/`callers`/`definition` в `mcp/service.py` |
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
- **Конфиг доски задач (`task_board`) — глобальный дефолт деплоя, не per-repo.** Подключение к доске (тип, MCP-сервер, `key_pattern`, `url_template`) одинаково для всех репозиториев команды, поэтому задаётся **один раз** в env reviewer-mcp (`TASK_BOARD_TYPE/MCP/KEY_PATTERN/URL_TEMPLATE` → `Settings.task_board_default()`), а не дублируется в `.review.yml` каждого репо. Приоритет: блок `task_board` из `.review.yml` целевой ветки **переопределяет** env-дефолт (`ReviewPolicy.from_settings` ставит дефолт, `load` накатывает yml); пустой `task_board:` в `.review.yml` **выключает** доску для конкретного репо. Серверный `review-pr` читает это через политику; клиентский скил `solve-task` читает одиночную задачу store-first — через MCP-тул `get_task()` из стора reviewer (после `sync_board`); board-MCP на стороне LLM остаётся фолбэком при промахе стора или для досок без REST-провайдера. Поэтому `.review.yml` **не обязателен** в каждом репо только ради доски.
- **Болк-синк задач — server-side ETL, не LLM (`sync_board`).** Скил `sync-tasks` теперь тонкий триггер: один вызов MCP-тула `sync_board(board, limit, purge_orphaned, keep_with_prs)` — `reviewer-mcp` сам ходит на доску по **REST** (`reviewer/tasks/boards/`, `TaskBoardProvider`; yougile — референс), нормализует задачи в `TaskBrief` (порт плейбука `task-context-yougile.md`, в Python) и индексирует через существующий `TaskService.index_batch`. LLM не перечисляет доску и не передаёт текст задач → синк стоит O(1) токенов. Инкрементальность — timestamp-watermark в `index_meta` (`repo=""`, `ref="tasks:<board>"`): повторный синк трогает ~0 задач; `--limit` отключает purge и продвижение курсора (частичный обход не даёт полного active-keys). Креды REST-доски — только в env reviewer-mcp (`TASK_BOARD_API_KEY`/`TASK_BOARD_API_BASE`), `board_config()` их не отдаёт. **Это разворачивает инвариант «reviewer Python никогда не трогает доску» — но только для болк-синка**; одиночное чтение задачи в `review-pr` по-прежнему идёт через board-MCP на стороне LLM; `solve-task` использует store-first (`get_task()`), board-MCP — фолбэк при промахе. Задачи **глобальны** (таблица `tasks` и граф `:Task` без repo-скоупа), поэтому курсор ключуется по доске, не по репо. **Скоуп задач по проекту (PRI-170):** хранилище задач (`tasks`, `:Task`) остаётся глобальным, но синк и выдача (`search_tasks`/`get_task`/`get_task_context`/обход графа) скоупятся по `task_board.project` из `.review.yml` репо (префикс кода, напр. `PRI`); пусто = всё. `sync_board(board_type, board)` ограничивает синк одним типом доски и проектом. Клиент-скилы передают `project` из `.review.yml`; сервер repo-агностичен.
- **SHA base-индекса** хранится в таблице `index_meta` (пишется при `reviewer index`). При каждом `prepare_review` (MCP) SHA сравнивается с `base_sha` PR и при расхождении автоматически досинхронизируются чанки изменившихся файлов через GitHub compare API. Граф (Neo4j) **также** инкрементально досинхронизируется в этом шаге (tree-sitter, repo-scoped, входящие `CALLS`-рёбра сохраняются, fail-soft). Полная точность (рёбра `IMPLEMENTS` + все `CALLS`) восстанавливается ручным `reviewer index` с SCIP.
- **Мульти-бранч base-индекс.** Каждая отслеживаемая ветка (`REVIEW_BRANCHES`, CSV, первая — первичная) имеет изолированный base-индекс: в Postgres `ref="base:<branch>"` (overlay PR остаётся `pr:N`), в Neo4j `:Symbol{repo, branch, id}` (составная уникальность `(repo, branch, id)`). PR ревьюится против индекса своей целевой ветки (`prq.base_ref`); PR в ветку вне списка ревью **пропускает** (`prepare_review` → `{"status":"skipped",...}`). `reviewer index --ref <branch>` строит индекс ветки; `reviewer search --branch <branch>` ищет по нему. Эмбеддинги переиспользуются между ветками по `content_hash` (экономия Voyage). Ветка-агностичные операции (CLI search, solve-task) идут по первичной ветке или текущей git-ветке клона. Миграция legacy-данных: `reviewer migrate-branches` (один раз после апгрейда).
- **Мульти-репо через `repo`-дискриминатор**: один деплой обслуживает N репозиториев через `repo` (`owner/name`) в Postgres (`chunks`/`index_meta`) и Neo4j (`:Symbol.repo`, составная уникальность `(repo, branch, id)`). Индексация: `reviewer index --repo owner/name` (или derive из git remote `origin` / `DEFAULT_REPO`). Граф задач `:Task` глобален — задача может покрывать несколько микросервисных репозиториев. Каждое ревью изолировано в рамках своего репо (без кросс-репо ретрива).
- **`reviewer check`** проверяет готовность окружения (ключи, Postgres, Neo4j, GitHub) без трат квот Voyage.
- **Глубина кластеризации сводок (`SUMMARY_CLUSTER_DEPTH`, дефолт 2)** — env-настройка деплоя для `/summarize-subsystems`: до скольких сегментов пути обрезается `cluster_key` подсистемы. Per-repo override — ключ `summary_cluster_depth` в `.review.yml` целевой ветки (резолвится server-side в `list_subsystem_clusters`/`index_subsystem_summary`). Смена depth меняет `cluster_key` → **полный пересбор всех сводок**; осиротевшие сводки старого depth вычищаются `prune_subsystem_summaries` на полном (uncapped) прогоне скилла (PRI-166).
- **Overlay удаляется автоматически** (`store.delete_ref("pr:N")`) — после `publish_review` эфемерный ref не остаётся в Postgres. При сбое prepare также чистится (fail-soft).
- **Наблюдаемость (`reviewer/web/`)**: каждый `publish_review` пишет в Postgres итоги прогона (`review_runs`/`review_findings`, гейт `REVIEW_HISTORY`) — fail-soft. Веб-админка (FastAPI `reviewer serve`) читает **ту же** БД.
- **MCP-сессия живёт в процессе сервера** между `prepare_review` и `publish_review` одного PR: `_Session(prepared, ctx)` в `MCPReviewService._sessions`. При повторном `prepare_review` для того же (repo, pr) сессия перезаписывается, старый VCS-провайдер закрывается (fail-soft).
- **Плагин** находится в `plugin/` в корне репозитория — это корень Claude Code-плагина для скилла `/rag-reviewer:reviewer_review-pr`.
- **Общие reference-блоки промптов** вынесены в `plugin/skills/_common/` (единый источник: `findings-schema.md`, `anti-hallucination.md`, `tool-usage.md`, `branch-selection.md`). Скиллы и reference-промпты подключают их маркером `<!-- include: _common/<file>.md -->` (путь от `plugin/skills/`), который LLM-оркестратор разворачивает verbatim при сборке промпта субагента; скилл-специфичные части остаются в самих скиллах. Соответствие findings-schema ↔ `Finding` (`reviewer/vcs/base.py`) и корректность сборки промптов охраняют guard-тесты в `tests/skills/`.
- **Мульти-платформа VCS (GitHub + GitLab).** Тип провайдера — свойство репо, не PR. `reviewer index` определяет платформу из `git remote` (`derive_vcs_from_remote`) и пишет в таблицу `repo_vcs(repo→provider,base_url)`. При ревью (API-only движок) `_create_vcs_provider` читает `repo_vcs` ДО любого API-вызова и выбирает `GitHubProvider`/`GitLabProvider`; токен — из ENV по платформе (`GITHUB_TOKEN`/`GITLAB_TOKEN`, `GITLAB_URL` для self-hosted). Фолбэк при пустом `repo_vcs` — `VCS_PROVIDER` (дефолт github), что сохраняет обратную совместимость. Секретов в `.review.yml` нет (нет блока `vcs:`).

## Соглашения

- Внешние сервисы (GitHub, Voyage, Postgres, Neo4j) изолированы за интерфейсами и мокаются в unit-тестах; реальные вызовы — только в integration/E2E.
- Коммиты: **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude). Стиль сообщений — Conventional Commits на русском (`feat(agent): …`, `fix(index): …`).
