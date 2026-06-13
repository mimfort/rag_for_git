# rag_for_git

Агент автоматического ревью pull/merge request'ов на основе **RAG + графа кода + Claude Code**.

На событие «появился/обновился PR» агент берёт дифф, собирает релевантный контекст **по всему репозиторию** (гибридный поиск + граф связей кода), прогоняет его через Claude Code-скилл с инструментами поиска (agentic RAG), отсеивает ложные срабатывания и постит результат обратно в GitHub: **inline-комментарии на строки диффа + сводку**.

> Статус: рабочий v1. Целевой язык анализа — **Python**. VCS — **GitHub** (за интерфейсом `VCSProvider`, под GitLab/др. заложена абстракция). Проверено вживую: ловит реальные баги, видит влияние на вызывающий код и существующие тесты.

---

## Содержание
- [Зачем это и в чём идея](#зачем-это-и-в-чём-идея)
- [Архитектура: как связаны части](#архитектура-как-связаны-части)
- [Как работает ревью (поток данных)](#как-работает-ревью-поток-данных)
- [Свежесть индекса на «живом» репозитории](#свежесть-индекса-на-живом-репозитории)
- [Быстрый старт](#быстрый-старт)
- [Использование (CLI)](#использование-cli)
- [Эксплуатация](#эксплуатация)
- [Пример ревью «от диффа до комментария»](#пример-ревью-от-диффа-до-комментария)
- [Конфигурация](#конфигурация)
- [Структура проекта](#структура-проекта)
- [Тесты](#тесты)
- [Ограничения и заметки](#ограничения-и-заметки)

---

## Зачем это и в чём идея

Обычные линтеры ловят синтаксис и стиль, но не видят **смысла и связей**: сломанный контракт функции, влияние правки на вызывающих, удалённую проверку, противоречие существующему тесту. Идея агента — дать LLM **тот же контекст, что у живого ревьюера**:

- **RAG** — найти по всему репозиторию похожий/связанный код семантически (вектора) и лексически (BM25);
- **Граф кода** — структурно подтянуть вызывающих/вызываемых/реализации/тесты изменённого символа;
- **LLM с инструментами** — рассуждать над диффом, дотягивая нужный код тулзами, и выносить замечания;
- **Verify-проход** — отсеять галлюцинации, не теряя реальных багов.

## Архитектура: как связаны части

```
                ┌──────────────────────────── reviewer (ядро-библиотека) ────────────────────────────┐
                │                                                                                      │
  GitHub PR ───▶│  VCSProvider (github.py)  ──дифф/файлы/патчи──▶  MCPReviewService                  │
  (owner/repo#N)│        ▲  публикация inline+сводка                     │                              │
                │        │                                               │ prepare_review               │
                │        │                                               ▼                              │
                │        │                         ┌──────────── retrieval/Retriever ───────────┐      │
                │        │                         │  гибрид-поиск          graph-expansion        │      │
                │        │                         │  ┌───────────────┐    ┌──────────────────┐    │      │
                │        │                         │  │ Postgres       │    │ Neo4j            │    │      │
                │        │                         │  │ (ParadeDB)     │    │ Symbol(path#fqn) │    │      │
                │        │                         │  │ pgvector(HNSW) │    │ -[:CALLS]-> (граф)│    │      │
                │        │                         │  │ + pg_search    │    │ (IMPLEMENTS: SCIP)│    │      │
                │        │                         │  │   (BM25, RRF)  │    │ expand 1–2 хопа   │    │      │
                │        │                         │  └──────▲────────┘    └─────────▲────────┘    │      │
                │        │                         │         │ chunks (vector+text)  │ узлы/рёбра  │      │
                │        │                         │         │                       │             │      │
                │        │                         │      Voyage embed/rerank   tree-sitter граф   │      │
                │        │                         └──────────────────┬──────────────────────────┘      │
                │        │                                            ▼ ContextPack                       │
                │        │                         Claude Code subagents (скилл /rag-reviewer:review-pr)  │
                │        │                           инструменты: search_code, get_related_symbols,       │
                │        │                           read_file, get_definition, find_callers,             │
                │        │                           get_changed_file_diff                                │
                │        └──────────────────── publish_review (gate/grounding/dedup/assemble) ◀─────────┘
                └──────────────────────────────────────────────────────────────────────────────────────┘

  Хранилища поднимаются в Docker:  Postgres/ParadeDB (:5433)  ·  Neo4j (:7687)
  Внешние API:  Voyage (эмбеддинги voyage-code-3 + reranker rerank-2.5)
```

Кратко, кто за что отвечает:

| Часть | Модуль | Роль |
|---|---|---|
| VCS-провайдер | `reviewer/vcs/` | получить PR/дифф/файлы, запостить ревью; маппинг строк диффа; идемпотентность |
| Индекс (RAG) | `reviewer/index/` | чанкинг (tree-sitter), эмбеддинги (Voyage), хранилище (pgvector+BM25), свежесть |
| Граф кода | `reviewer/graph/` | построение рёбер `CALLS` + `IMPLEMENTS` (SCIP-бэкенд) или только `CALLS` (tree-sitter); оркестрация в `backend.py`; хранение и обход в Neo4j |
| Ретрив | `reviewer/retrieval/` | гибрид (RRF) + graph-expansion + Voyage rerank → контекст |
| Инструменты | `reviewer/tools/` | `search_code`, `get_related_symbols`, `read_file`, `get_definition`, `find_callers`, `get_changed_file_diff` |
| MCP-сервис | `reviewer/mcp/` | `MCPReviewService`: prepare/tool-вызовы/publish; управление сессиями PR |
| Сервис | `reviewer/services/` | `ReviewService.prepare`: ingest PR, overlay, units |
| Агент | `reviewer/agent/` | state (ReviewUnit) · assemble · dedup |
| LLM утилиты | `reviewer/llm/` | `_retry.py` (retry/backoff для Voyage) |
| Политика | `reviewer/policy/` | гейтинг findings (категория/severity/confidence/пути) |

**Единый ключ связи** между RAG и графом — `node_id = "path#fqn"` (напр. `rag/embedder.py#VoyageEmbedder.embed_query`). И чанк в Postgres, и узел в Neo4j используют его, поэтому graph-expansion и ретрив чанков «сшиваются» без дополнительной маппинг-таблицы.

## Как работает ревью (поток данных)

Ревью запускается скиллом `/rag-reviewer:review-pr` в Claude Code. Поток на один PR:

```
──────────────── prepare_review (MCP → MCPReviewService) ─────────────────────
1. ingest      GitHub: PR (base_sha, head_sha, base_ref) + изменённые файлы с патчами
                  │
2. overlay     изменённые .py → чанкинг (tree-sitter) → эмбеддинг (Voyage) →
               upsert в Postgres под ref="pr:N"  (content-hash дедуп)
                  │
3. plan        дифф → review-units (по файлу): {path, node_ids изменённых символов, patch}
               → payload скиллу: юниты/политика/патчи
                  │
────────── analyze: Claude subagents (скилл /rag-reviewer:review-pr) ──────────
4. analyze     Subagents в tool-loop по каждому файлу:
                 • search_code(query)        → Retriever:
                        embed_query (Voyage) → гибрид-поиск по (base \ changed ∪ overlay):
                          pgvector ANN  +  pg_search BM25  → слияние RRF
                        + graph.expand(изменённые символы) → Neo4j callers/callees (impl/тесты при наличии рёбер)
                        + Voyage rerank → top-N  → ContextPack (код с цитатами path:line)
                 • get_related_symbols(node) → связанные символы из графа
                 • read_file, get_definition, find_callers, get_changed_file_diff
               → findings (JSON): category, severity, line, message, suggestion, confidence
                  │  (findings аккумулируются со всех файлов)
                  │
─────────── publish_review (MCP → MCPReviewService) ──────────────────────────
5. gate        policy.gate: категория включена? severity ≥ порога? confidence ≥ порога?
               путь не в ignore?
                  │
6. grounding   уточнение номера строки по дословной code_quote (анти-галлюцинация)
               + dedup по fingerprint (схлопываем одинаковые находки)
                  │
7. assemble    findings → разделение:
                 • строка попадает в дифф → inline-комментарий (RIGHT/LEFT)
                 • иначе → пункт в сводку (с ссылкой file:line)
               + кап max_comments + идемпотентность по фингерпринту (не дублировать на повторном push)
                  │
8. publish     GitHubProvider: один review = сводка + массив inline-комментариев
```

Ключевые свойства:
- **agentic RAG** — ретрив только засеивает контекст; LLM сам дотягивает нужный код тулзами.
- **graph + RAG вместе** — вектора находят «похожее по смыслу», граф добавляет «структурно связанное» (кто сломается), что эмбеддинги часто упускают.
- **inline только на строках диффа** — GitHub разрешает комментарии лишь на изменённых/контекстных строках хунка; остальное уходит в сводку (инвариант зашит в `assemble`).
- **идемпотентность** — каждый комментарий помечен скрытым фингерпринтом `<!-- ai-review:hash -->`; повторный прогон не плодит дубликаты.

## Свежесть индекса на «живом» репозитории

Код меняется с каждым push'ем, а полный реиндекс большого репо дорог. Решение — **стабильная база + content-hash дедуп + overlay на PR**:

- **`ref="base"`** — персистентный индекс целевой ветки. Обновляется инкрементально (`reviewer index`): чанкуются только изменённые файлы, эмбеддятся только чанки с новым `content_hash`.
- **`ref="pr:N"`** — эфемерный overlay: только изменённые файлы PR на его HEAD.
- **На запросе**: `retrieval = (base, где path ∉ изменённых) ∪ overlay`. То есть для изменённых файлов агент видит **новую** версию, для остального — стабильную базу. Это и есть условие «находить произвольный релевантный код по всему репо, но по актуальной версии».

## Быстрый старт

Нужны: Python 3.11–3.13, Docker, ключ Voyage, GitHub-токен, Claude Code с плагином.

```bash
# 1. зависимости и инфраструктура
git clone https://github.com/mimfort/rag_for_git && cd rag_for_git
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker compose up -d                 # Postgres/ParadeDB (:5433) + Neo4j (:7687) + web-админка (:8000)

# 2. конфиг
cp .env.example .env                 # заполнить VOYAGE_API_KEY, GITHUB_TOKEN
```

Где взять ключи:
- **Voyage** (`VOYAGE_API_KEY`): https://dashboard.voyageai.com/ — есть 200M бесплатных токенов; чтобы снять лимит 3 RPM / 10K TPM, привяжите карту (списания идут только сверх бесплатного пула; auto-recharge можно держать выключенным).
- **GitHub** (`GITHUB_TOKEN`): PAT с правами *Pull requests: Read and write* + *Contents: Read* (fine-grained) или scope `repo` (classic). Быстрый вариант для своих репо: `gh auth token`.

## Использование

После `pip install -e .` доступны команды `reviewer` (CLI) и `reviewer-mcp` (MCP-сервер для плагина).

### CLI

```bash
# Проиндексировать базу целевой ветки локального клона (вектора + граф).
# Делается один раз и обновляется инкрементально; даёт RAG/графу контекст всего репо.
reviewer index /path/to/repo --ref main

# Диагностический гибрид-поиск по базе (проверить, что индекс работает).
reviewer search "token verification"
```

### Ревью через Claude Code-плагин

Ревью запускается через скилл `/rag-reviewer:review-pr` в Claude Code:

```bash
# 1. Убедиться, что MCP-сервер добавлен в Claude Code-настройки
#    (reviewer-mcp / plugin/ как корень плагина)

# 2. Открыть репозиторий в Claude Code и вызвать скилл:
/rag-reviewer:review-pr owner/repo#42
```

Плагин (`plugin/`) вызывает `prepare_review` (через MCP), затем запускает subagents с инструментами поиска `search_code`, `get_related_symbols`, `read_file` и т.д., наконец `publish_review` (через MCP) постит результат в GitHub.

Типичный сценарий:

```bash
git clone https://github.com/ORG/REPO /tmp/REPO
reviewer index /tmp/REPO --ref main        # построить базу+граф
# в Claude Code: /rag-reviewer:review-pr ORG/REPO#42   # ревью PR #42
```

> Ревью работает и без предварительного `index` — тогда контекст ограничен диффом и overlay (RAG/граф «тонкие»). Для полноценного анализа влияния на весь репозиторий запустите `index` по целевой ветке.

## Эксплуатация

### Диагностика и первый запуск

```bash
# Проверить готовность окружения: ключи, Postgres, Neo4j, GitHub.
# Выводит ✓/✗ по каждому пункту; exit 1 при любой проблеме.
reviewer check
```

Прогон без публикации: в скилле передайте `--dry-run` — `publish_review` соберёт отчёт, не постя в GitHub.

### Веб-админка наблюдаемости

Каждый `publish_review` записывает прогон в Postgres (таблицы `review_runs` / `review_findings`):
репозиторий/PR, модель, тайминги, статус, находки с вердиктами и фактом публикации. Запись
**fail-soft** (сбой лога не ломает ревью) и гейтится `REVIEW_HISTORY` (дефолт `true`). Стоимости
в записи нет — LLM-вызовы идут по подписке Claude Code.

Веб-админка (FastAPI + React/Vite SPA) показывает историю прогонов, агрегаты (% отсева gate,
графики во времени, находки по категориям/severity) и детали каждого прогона — с drill-down по
находкам.

**Через Docker (без ручных шагов).** Сервис `web` в `docker-compose.yml` сам собирает фронт
(multi-stage: node → python) и поднимает FastAPI, читая ту же БД, что пишет `publish_review`:

```bash
docker compose up -d                 # поднимает Postgres + Neo4j + web-админку
# открыть http://127.0.0.1:8000
```

**На хосте (для разработки фронта).** Альтернатива без Docker:

```bash
pip install -e ".[web]"
cd web/frontend && npm install && npm run build && cd -
reviewer serve                       # http://127.0.0.1:8000 (опции: --host/--port)

# hot-reload фронта (в отдельном терминале, при запущенном reviewer serve):
cd web/frontend && npm run dev       # http://localhost:5173, /api проксируется на :8000
```

API: `GET /api/runs` (список с фильтрами repo/status, пагинация), `GET /api/runs/{id}`
(прогон + находки), `GET /api/runs/{id}/trace` (пошаговый трейс), `GET /api/stats?days=N` (агрегаты).

> Трейс пишется только для **новых** прогонов (инструментация forward-only) — у прогонов,
> сделанных до включения фичи, вкладка «Трейс» покажет пустое состояние.

### Свежесть base-индекса

`reviewer index` фиксирует SHA проиндексированного ref в таблице `index_meta`. При каждом `prepare_review` сверяется этот SHA с `base_sha` PR: если есть расхождение — автоматически досинхронизирует чанки изменившихся файлов через GitHub compare API (без пересборки всего индекса). Граф кода (Neo4j) обновляется **только** при явном `reviewer index` — не при ревью.

### Капы и флаги

| Переменная | Дефолт | Назначение |
|---|---|---|
| `REVIEW_MAX_FILES` | 50 | максимум файлов .py на ревью; лишние — в сводку как пропущенные |
| `REVIEW_SKIP_DRAFTS` | `true` | не ревьюить draft-PR |
| `REVIEW_MAX_COMMENTS` | 25 | кап inline-комментариев на ревью |

### Устойчивость к ошибкам

- Транзиентные ошибки LLM (HTTP 429/5xx) ретраятся с экспоненциальным backoff.
- Ошибка анализа одного файла не прерывает ревью — файл помечается как неудачный и попадает в сводку.

### Ограничение: один репозиторий на инстанс

Индекс не имеет namespace по репозиторию. Один деплой (одна БД Postgres + Neo4j) рассчитан на один репозиторий. Для нескольких репозиториев — отдельные инстансы с разными `PG_DSN` / `NEO4J_URI`.

## Пример ревью «от диффа до комментария»

PR удаляет «лишнюю», на первый взгляд, проверку в `rag/embedder.py`:

```diff
@@ def _embed(self, texts, input_type):
         items = sorted(data["data"], key=lambda item: item["index"])
         vectors = [item["embedding"] for item in items]
-
-        for i, vec in enumerate(vectors):
-            if len(vec) != self._dim:
-                raise RuntimeError(f"Эмбеддинг {i} ... ожидается {self._dim} ...")
         return vectors
```

Дифф выглядит безобидно («упростить»). Агент:
1. строит overlay из новой версии файла, ретривом и графом подтягивает связанный код (`_embed` вызывается из `embed_query`/`embed_documents`, те — из `Retriever.retrieve`, `ingest_file`, `_index_text`) и существующие тесты;
2. LLM понимает, что удалён fail-fast контракт размерности;
3. verify подтверждает, политика пропускает (severity=medium ≥ порога, confidence ≥ 0.5).

Итоговый inline-комментарий на PR:

> **[correctness/medium]** Удалена fail-fast проверка размерности эмбеддингов. Ломается тест `test_embed_dimension_mismatch_raises`. Векторы неверной размерности пройдут дальше в `Retriever.retrieve`, `ingest_file`, `_index_text`, где упадут позже с неинформативной ошибкой (или тихо деградируют при смене модели).
>
> 💡 _Предложение:_ вернуть проверку `len(vec) != self._dim` перед `return vectors`.

Обрати внимание: упоминание **конкретного существующего теста** и **вызывающих** — это результат RAG (поиск по базе) и графа (обход связей), а не только диффа.

> Агент **только комментирует** — он никогда не меняет и не откатывает код сам. Предложения могут приходить как **applyable** GitHub-блоки `suggestion` (кнопка «Apply»), но безопасно: блок ставится только когда модель даёт точную замену конкретного непрерывного диапазона строк диффа (диапазон целиком в RIGHT-части, без пересечений; иначе — текстовый совет). Поведение задаётся `REVIEW_SUGGESTIONS` (`apply`/`text`).

## Конфигурация

Всё ключевое — через `.env` (см. `.env.example` с комментариями). Главное:

| Переменная | Назначение |
|---|---|
| `VOYAGE_API_KEY` | ключ Voyage (эмбеддинги + ранжирование) |
| `GITHUB_TOKEN` | токен GitHub (PAT: *Pull requests: RW* + *Contents: R*) |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | модель Voyage и размерность (= колонке `vector(N)`; смена ⇒ реиндекс) |
| `RERANK_MODEL` | модель реранкера Voyage |
| `REVIEW_SEVERITY_THRESHOLD` | мин. важность: `low/medium/high/critical` |
| `REVIEW_MIN_CONFIDENCE` | отбрасывать findings ниже уверенности (0..1) |
| `REVIEW_MAX_COMMENTS` | кап inline-комментариев |
| `REVIEW_CATEGORIES` | CSV вайтлист категорий (пусто = все) |
| `REVIEW_SUGGESTIONS` | `apply` = applyable `suggestion`-блоки (кнопка «Apply»), `text` = только текстовые советы |
| `REVIEW_MAX_FILES` | кап файлов PR; лишние — в сводку как пропущенные |
| `REVIEW_OUTPUT_LANGUAGE` | язык текста находок в публикуемом ревью (дефолт `ru`) |
| `REVIEW_SKIP_DRAFTS` | `true` = не ревьюить draft-PR |
| `REVIEW_HISTORY` | `true` = сохранять историю прогонов в Postgres |
| `PG_DSN`, `NEO4J_URI/USER/PASSWORD`, `GITHUB_TOKEN` | подключения и доступ |

Эфемерный overlay `pr:N` удаляется из Postgres автоматически по окончании `publish_review`.

**Политика per-repo.** Файл `.review.yml` в **целевой ветке** репозитория переопределяет env-дефолты (PR не может ослабить собственное ревью):

```yaml
categories: { correctness: true, security: true, performance: true, style: false, requirements: true }
severity_threshold: medium
min_confidence: 0.5
paths: { ignore: ["**/migrations/**", "vendor/**"] }
max_comments: 25

# Контекст задачи (опц.): читать задачу с доски и проверять соответствие требованиям.
# Доску (MCP) подключает пользователь на стороне сессии Claude Code; плагин её не бандлит.
task_board:
  type: yougile          # yougile | jira — выбирает плейбук скилла
  mcp: yougile           # имя подключённого MCP-сервера доски (тулы зовутся mcp__<mcp>__*)
  key_pattern: "[A-Z]+-\\d+"   # опц.; дефолт такой же (подходит Yougile PRI-34/ID-34 и Jira PROJ-123)
  # url_template: "https://yougile.com/...{id}"  # опц.; ссылка на задачу в сводке ({id}/{key})
```

**Контекст задачи (фаза 2).** Если задан `task_board` и в PR (title/body/ветка) найден ключ
по `key_pattern`, скилл читает задачу с доски через её MCP и запускает проверку соответствия —
новая категория находок `requirements` (включена по умолчанию). Находки без конкретной строки
диффа уходят в сводку. Доска не настроена, ключ не найден или MCP недоступен → ревью работает
как обычно, без деградации.

## Структура проекта

```
reviewer/
  config/      Settings (pydantic-settings): env → пороги ревью, хранилища
  vcs/         VCSProvider + github.py (httpx) · diff.py (строки, доступные для inline)
  index/       chunker(tree-sitter) · embeddings(Voyage) · reranker · store(pgvector+pg_search/RRF) · freshness
  graph/       builder(tree-sitter call-graph) · scip(точный парсер SCIP) · backend(оркестратор бэкенда) · store(Neo4j)
  retrieval/   Retriever: гибрид + graph-expansion + rerank → ContextPack
  llm/         _retry.py (retry/backoff для Voyage)
  tools/       инструменты агента (search_code, get_related_symbols, read_file, get_definition, …)
  agent/       state (ReviewUnit) · assemble · dedup
  mcp/         MCPReviewService: prepare/tool-вызовы/publish; MCP-сервер (server.py)
  services/    ReviewService.prepare: ingest PR, overlay, units
  policy/      ReviewPolicy: env-дефолты + .review.yml + гейтинг
  entrypoints/ cli.py (index / search / check / serve)
  web/         FastAPI + React/Vite SPA — веб-админка наблюдаемости
  app.py       сборка зависимостей из Settings
plugin/        Claude Code-плагин (скилл /rag-reviewer:review-pr)
docker-compose.yml   ParadeDB (pgvector+pg_search) + Neo4j + web-админка
```

## Тесты

```bash
.venv/bin/pytest -q                 # unit (быстрые, на фейках; внешние API не дёргают)
.venv/bin/pytest -m integration     # integration: нужны поднятые Postgres/Neo4j + ключ Voyage
```

Внешние сервисы изолированы за интерфейсами и мокаются в unit-тестах; реальные вызовы — только в integration/E2E.

## Ограничения и заметки

- **v1 — только Python** (чанкер и граф). Другие языки — за тем же интерфейсом `GraphIndexer`/чанкера.
- **Граф кода — два бэкенда** (настройка `GRAPH_BACKEND=auto|scip|treesitter`):
  - **SCIP** (`@sourcegraph/scip-python`, npm): точный type-aware граф с рёбрами `CALLS` + `IMPLEMENTS`; требует `scip-python` в PATH; индексирует через временный git worktree.
  - **tree-sitter** (fallback): быстрый, без внешних зависимостей, только `CALLS` по имени.
  - Режим `auto` (по умолчанию): SCIP если найден, иначе tree-sitter; при ошибке SCIP — автооткат на tree-sitter с предупреждением.
- **Voyage free tier** = 3 RPM / 10K TPM: полная индексация большого репо требует привязанной карты (бесплатные 200M токенов сохраняются) либо медленной инкрементальной индексации; в коде есть retry/backoff.
- **VCS** — пока GitHub; GitLab/др. добавляются реализацией `VCSProvider`.
- **Запуск** — пока CLI; webhook-сервис добавляется как точка входа (ядро уже библиотека).
