# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## О проекте

`rag_for_git` — агент автоматического ревью pull/merge request'ов: **RAG (гибридный поиск) + граф кода + LLM с инструментами (agentic RAG)**. На вход — PR на GitHub, на выход — inline-комментарии на строки диффа + сводка. Целевой язык анализа — Python; VCS — GitHub (за интерфейсом `VCSProvider`). Подробный разбор архитектуры и потока данных — в `README.md` (написан на русском, сверен с кодом).

Язык проекта — **русский**: комментарии, докстринги, сообщения CLI, промпты LLM. Сохраняй этот стиль в новом коде.

## Команды

```bash
# Установка (Python 3.11–3.13)
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Инфраструктура: ParadeDB (host-порт 5433) + Neo4j (7687)
docker compose up -d

# Конфиг: ключи Voyage/OpenRouter/GitHub
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
reviewer review owner/repo 123             # отревьюить PR и запостить inline + сводку
reviewer review owner/repo 123 --dry-run  # прогон без публикации, вывод в консоль
```

`pytest` по умолчанию **исключает** integration-тесты (`addopts = -m 'not integration'` в `pyproject.toml`) — маркер `integration` помечает тесты, требующие поднятых Postgres/Neo4j.

## Архитектура

Ядро — библиотека `reviewer/`, собираемая в `reviewer/app.py::build_components(settings)` из `Settings` (pydantic-settings, `.env`). Точка входа — `reviewer/entrypoints/cli.py` (Click).

Поток `reviewer review owner/repo N`: **подготовка в CLI** (шаги 1–2) → **LangGraph-граф** (шаги 3–7).

1. **ingest** (CLI) — `GitHubProvider` тянет PR (base/head sha) + изменённые файлы с патчами.
2. **overlay** (CLI) — изменённые `.py` чанкуются (tree-sitter) и эмбеддятся (Voyage) в `ref="pr:N"`.
3. **plan** → `Send` fan-out: каждый файл ревьюится параллельно как `ReviewUnit`.
4. **analyze** — `LLMAnalyzer`: tool-loop (`search_code`, `get_related_symbols`) поверх `Retriever`, затем структурированный JSON findings.
5. **verify** — `LLMVerifier` (recall-safe, fail-open) + `ReviewPolicy.gate` (категория/severity/confidence/пути).
6. **assemble** — findings → inline (строка в диффе) или сводка; кап `max_comments` + идемпотентность.
7. **publish** — `GitHubProvider`: один review = сводка + массив inline-комментариев.

Граф LangGraph определён в `reviewer/agent/graph.py`; узлы — фабрики `make_*_node(deps)` в `reviewer/agent/nodes.py`, прокидывающие `Deps` через замыкания.

### Модули

| Модуль | Роль |
|---|---|
| `reviewer/vcs/` | `VCSProvider` + `github.py` (httpx); `diff.py` — какие строки диффа доступны для inline |
| `reviewer/index/` | `chunker` (tree-sitter) · `embeddings`/`reranker` (Voyage) · `store` (pgvector + pg_search/BM25, RRF) · `freshness` (base/overlay) |
| `reviewer/graph/` | `builder` (tree-sitter call-graph) · `scip` (парсер SCIP) · `backend` (оркестратор бэкенда: SCIP / tree-sitter) · `store` (Neo4j) |
| `reviewer/retrieval/` | `Retriever`: гибрид (RRF) + graph-expansion + Voyage rerank → `ContextPack` |
| `reviewer/llm/` | `OpenRouterProvider` (provider-блок/max_price/fallback через `extra_body`) · `BudgetTracker` |
| `reviewer/tools/` | инструменты агента (`search_code`, `get_related_symbols`) |
| `reviewer/agent/` | `state` · `nodes` · `graph` · `analyzer` · `prompts` |
| `reviewer/policy/` | `ReviewPolicy`: env-дефолты + `.review.yml` из целевой ветки + гейтинг |

### Ключевые инварианты

- **`node_id = "path#fqn"`** — единый ключ связи RAG↔граф. И чанк в Postgres (`store.py`), и узел в Neo4j используют его, поэтому graph-expansion и ретрив чанков сшиваются без маппинг-таблицы.
- **Свежесть индекса (base + overlay).** `ref="base"` — персистентный индекс целевой ветки (инкрементальный, дедуп по `content_hash`). `ref="pr:N"` — эфемерный overlay изменённых файлов PR. На запросе ретрив = `(base где path ∉ changed) ∪ overlay` — для изменённых файлов агент видит новую версию, для остального стабильную базу. Логика в `index/freshness.py` и `WHERE`-условиях `store.hybrid_search`/`fetch_nodes`.
- **inline только на строках диффа.** GitHub разрешает комментарии лишь на изменённых/контекстных строках хунка; остальное уходит в сводку (зашито в `assemble`, см. `commentable_lines`).
- **applyable `suggestion`-блок** ставится только при безопасных инвариантах (`_can_apply` в `nodes.py`): режим `apply`, точная замена, весь диапазон в RIGHT-части диффа, без пересечений. Иначе — текстовый совет.
- **Идемпотентность** — каждый комментарий помечен скрытым фингерпринтом `<!-- ai-review:hash -->`; повторный прогон не плодит дубликаты.

## Неочевидные факты (не выводятся из кода)

- **ParadeDB слушает host-порт 5433**, а не 5432 (на машине разработчика 5432 занят нативным Postgres). `PG_DSN` по умолчанию указывает на 5433.
- **Модель-агностичный разбор JSON.** `analyzer.py` НЕ использует langchain `with_structured_output` — некоторые модели OpenRouter (напр. minimax) возвращали с ним 0 findings. Вместо этого findings/вердикты вытаскиваются из обычного текста ответа (`_extract_json`), а `verify` работает fail-open (при невозможности разобрать вердикт — оставляет находку). Не «чини» это обратно на structured output.
- **Граф кода — два бэкенда.** Оркестратор `graph/backend.py` выбирает бэкенд через `GRAPH_BACKEND` (auto|scip|treesitter):
  - **SCIP** (`scip-python`, npm `@sourcegraph/scip-python`) — точный type-aware граф, рёбра CALLS и IMPLEMENTS, требует `scip-python` в PATH. Индексация выполняется через временный git worktree на `ref` (`add_worktree`/`remove_worktree` в `gitutil.py`).
  - **tree-sitter** (`graph/builder.py`) — быстрый fallback без внешних зависимостей, только CALLS по имени.
  - Режим `auto` (по умолчанию): если `scip-python` найден в PATH — SCIP, иначе tree-sitter. При `backend=scip` сбой пробрасывается; при `auto` — откат на tree-sitter с `log.warning`. Команда `reviewer index` полностью перестраивает граф (clear + upsert), чтобы рёбра разных бэкендов не смешивались.
- **Voyage free tier = 3 RPM / 10K TPM.** TPM — главный блокер: полный `reviewer index` большого репо упирается в лимит и троттлится; есть retry/backoff (`index/_retry.py`). Ревью одного PR (overlay + query-эмбеддинги) в лимит укладывается.
- **`.review.yml` берётся из целевой (base) ветки**, не из PR — PR не может ослабить собственное ревью.
- **SHA base-индекса** хранится в таблице `index_meta` (пишется при `reviewer index`). При каждом `reviewer review` CLI сравнивает его с `base_sha` PR и при расхождении автоматически досинхронизирует чанки изменившихся файлов через GitHub compare API. Граф (Neo4j) обновляется **только** при явном `reviewer index`.
- **Индекс single-repo**: нет namespace по репозиторию — один инстанс (одна БД) на один репозиторий. Несколько репо требуют отдельных деплоев с разными `PG_DSN`/`NEO4J_URI`.
- **`reviewer check`** проверяет готовность окружения (ключи, Postgres, Neo4j, GitHub) без трат квот Voyage/OpenRouter. **`--dry-run`** для `reviewer review` прогоняет полный анализ без публикации в GitHub.
- **Overlay удаляется автоматически** (`c.store.delete_ref("pr:N")` в `finally`-блоке CLI) — после ревью эфемерный ref не остаётся в Postgres.
- **verify может работать на отдельной дешёвой модели** (`OPENROUTER_MODEL_VERIFY`); prompt caching включён по умолчанию (`OPENROUTER_PROMPT_CACHE=true`) и передаётся через `cache_control` во все три этапа (analyze/verify/synthesize) — экономит input-токены на длинных tool-loop'ах.

## Соглашения

- Внешние сервисы (GitHub, Voyage, Postgres, Neo4j, OpenRouter) изолированы за интерфейсами и мокаются в unit-тестах; реальные вызовы — только в integration/E2E.
- Коммиты: **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude). Стиль сообщений — Conventional Commits на русском (`feat(agent): …`, `fix(index): …`).
