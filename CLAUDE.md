# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## О проекте

`rag_for_git` — агент автоматического ревью pull/merge request'ов: **RAG (гибридный поиск) + граф кода + Claude Code-плагин**. На вход — PR на GitHub, на выход — inline-комментарии на строки диффа + сводка. Целевой язык анализа — Python; VCS — GitHub (за интерфейсом `VCSProvider`). Подробный разбор архитектуры и потока данных — в `README.md` (написан на русском, сверен с кодом).

Язык проекта — **русский**: комментарии, докстринги, сообщения CLI. Сохраняй этот стиль в новом коде.

## Команды

```bash
# Установка (Python 3.11–3.13)
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Инфраструктура: ParadeDB (5433) + Neo4j (7687) + web-админка наблюдаемости (:8000, сервис web)
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
reviewer check                             # проверить готовность окружения (ключи, Postgres, Neo4j, GitHub)
reviewer index /path/to/repo --ref main   # построить/обновить base-индекс (вектора + граф) из локального клона
reviewer search "token verification"       # диагностический гибрид-поиск по base-индексу
reviewer serve                             # веб-админка наблюдаемости на хосте (история прогонов, находки)

# MCP-сервер (для Claude Code-плагина)
reviewer-mcp                               # запустить MCP-сервер (используется плагином)

# Ревью через Claude Code-плагин:
# 1. Открыть репозиторий как проект в Claude Code
# 2. Использовать скилл /rag-reviewer:review-pr (из plugin/)
#    Плагин вызывает prepare_review → analyze (Claude subagents) → publish_review

# Проще: `docker compose up -d` поднимает админку как сервис web (:8000) — фронт собирается в образе.
# На хосте (для разработки фронта): pip install -e ".[web]" && (cd web/frontend && npm install && npm run build) && reviewer serve
```

`pytest` по умолчанию **исключает** integration-тесты (`addopts = -m 'not integration'` в `pyproject.toml`) — маркер `integration` помечает тесты, требующие поднятых Postgres/Neo4j.

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
| `reviewer/tools/` | инструменты MCP-агента (`search_code`, `get_related_symbols`, `read_file`, `get_definition`, `find_callers`, `get_changed_file_diff`) |
| `reviewer/agent/` | `state` (ReviewUnit) · `assemble` · `dedup` |
| `reviewer/mcp/` | `MCPReviewService` — сервисный слой MCP (prepare/tools/publish/history) |
| `reviewer/services/` | `ReviewService.prepare` — подготовка PR (ingest + overlay + policy + units) |
| `reviewer/policy/` | `ReviewPolicy`: env-дефолты + `.review.yml` из целевой ветки + гейтинг |

### Ключевые инварианты

- **`node_id = "path#fqn"`** — единый ключ связи RAG↔граф. И чанк в Postgres (`store.py`), и узел в Neo4j используют его, поэтому graph-expansion и ретрив чанков сшиваются без маппинг-таблицы.
- **Свежесть индекса (base + overlay).** `ref="base"` — персистентный индекс целевой ветки (инкрементальный, дедуп по `content_hash`). `ref="pr:N"` — эфемерный overlay изменённых файлов PR. На запросе ретрив = `(base где path ∉ changed) ∪ overlay` — для изменённых файлов агент видит новую версию, для остального стабильную базу. Логика в `index/freshness.py` и `WHERE`-условиях `store.hybrid_search`/`fetch_nodes`.
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
- **SHA base-индекса** хранится в таблице `index_meta` (пишется при `reviewer index`). При каждом `prepare_review` (MCP) SHA сравнивается с `base_sha` PR и при расхождении автоматически досинхронизируются чанки изменившихся файлов через GitHub compare API. Граф (Neo4j) обновляется **только** при явном `reviewer index`.
- **Индекс single-repo**: нет namespace по репозиторию — один инстанс (одна БД) на один репозиторий. Несколько репо требуют отдельных деплоев с разными `PG_DSN`/`NEO4J_URI`.
- **`reviewer check`** проверяет готовность окружения (ключи, Postgres, Neo4j, GitHub) без трат квот Voyage.
- **Overlay удаляется автоматически** (`store.delete_ref("pr:N")`) — после `publish_review` эфемерный ref не остаётся в Postgres. При сбое prepare также чистится (fail-soft).
- **Наблюдаемость (`reviewer/web/`)**: каждый `publish_review` пишет в Postgres итоги прогона (`review_runs`/`review_findings`, гейт `REVIEW_HISTORY`) — fail-soft. Веб-админка (FastAPI `reviewer serve` или сервис `web` в docker-compose) читает **ту же** БД.
- **MCP-сессия живёт в процессе сервера** между `prepare_review` и `publish_review` одного PR: `_Session(prepared, ctx)` в `MCPReviewService._sessions`. При повторном `prepare_review` для того же (repo, pr) сессия перезаписывается, старый VCS-провайдер закрывается (fail-soft).
- **Плагин** находится в `plugin/` в корне репозитория — это корень Claude Code-плагина для скилла `/rag-reviewer:review-pr`.

## Соглашения

- Внешние сервисы (GitHub, Voyage, Postgres, Neo4j) изолированы за интерфейсами и мокаются в unit-тестах; реальные вызовы — только в integration/E2E.
- Коммиты: **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude). Стиль сообщений — Conventional Commits на русском (`feat(agent): …`, `fix(index): …`).
