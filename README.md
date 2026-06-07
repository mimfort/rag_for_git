# rag_for_git

Агент автоматического ревью pull/merge request'ов на основе **RAG + графа кода + LLM**.

На событие «появился/обновился PR» агент берёт дифф, собирает релевантный контекст **по всему репозиторию** (гибридный поиск + граф связей кода), прогоняет его через LLM из OpenRouter с инструментами (agentic RAG), отсеивает ложные срабатывания и постит результат обратно в GitHub: **inline-комментарии на строки диффа + сводку**.

> Статус: рабочий v1. Целевой язык анализа — **Python**. VCS — **GitHub** (за интерфейсом `VCSProvider`, под GitLab/др. заложена абстракция). Проверено вживую: ловит реальные баги, видит влияние на вызывающий код и существующие тесты.

---

## Содержание
- [Зачем это и в чём идея](#зачем-это-и-в-чём-идея)
- [Архитектура: как связаны части](#архитектура-как-связаны-части)
- [Как работает ревью (поток данных)](#как-работает-ревью-поток-данных)
- [Свежесть индекса на «живом» репозитории](#свежесть-индекса-на-живом-репозитории)
- [Быстрый старт](#быстрый-старт)
- [Использование (CLI)](#использование-cli)
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
  GitHub PR ───▶│  VCSProvider (github.py)  ──дифф/файлы/патчи──▶  Agent (LangGraph)                   │
  (owner/repo#N)│        ▲  публикация inline+сводка                     │                              │
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
                │        │                         LLM (OpenRouter, модель/цена/роутинг из env)           │
                │        └────────────────────────  analyze → verify → assemble  ◀──────────────────────┘
                └──────────────────────────────────────────────────────────────────────────────────────┘

  Хранилища поднимаются в Docker:  Postgres/ParadeDB (:5433)  ·  Neo4j (:7687)
  Внешние API:  Voyage (эмбеддинги voyage-code-3 + reranker rerank-2.5)  ·  OpenRouter (LLM)
```

Кратко, кто за что отвечает:

| Часть | Модуль | Роль |
|---|---|---|
| VCS-провайдер | `reviewer/vcs/` | получить PR/дифф/файлы, запостить ревью; маппинг строк диффа; идемпотентность |
| Индекс (RAG) | `reviewer/index/` | чанкинг (tree-sitter), эмбеддинги (Voyage), хранилище (pgvector+BM25), свежесть |
| Граф кода | `reviewer/graph/` | построение рёбер `CALLS` (IMPLEMENTS — опц. через SCIP), хранение и обход в Neo4j |
| Ретрив | `reviewer/retrieval/` | гибрид (RRF) + graph-expansion + Voyage rerank → контекст |
| LLM | `reviewer/llm/` | OpenRouter-провайдер (модель/потолок цены/роутинг из env) + бюджет |
| Инструменты | `reviewer/tools/` | `search_code`, `get_related_symbols` для агента |
| Агент | `reviewer/agent/` | LangGraph-граф: plan→analyze→verify→assemble→publish (ingest/overlay — в CLI до графа) |
| Политика | `reviewer/policy/` | гейтинг findings (категория/severity/confidence/пути) |

**Единый ключ связи** между RAG и графом — `node_id = "path#fqn"` (напр. `rag/embedder.py#VoyageEmbedder.embed_query`). И чанк в Postgres, и узел в Neo4j используют его, поэтому graph-expansion и ретрив чанков «сшиваются» без дополнительной маппинг-таблицы.

## Как работает ревью (поток данных)

Команда `reviewer review owner/repo N` сначала готовит данные в CLI-entrypoint'е (шаги 1–2), затем запускает **LangGraph-граф** (шаги 3–7: `plan→analyze→verify→assemble→publish`). Поток на один PR:

```
──────────────── подготовка в CLI (entrypoints/cli.py, до графа) ────────────────
1. ingest      GitHub: PR (base_sha, head_sha, base_ref) + изменённые файлы с патчами
                  │
2. overlay     изменённые .py → чанкинг (tree-sitter) → эмбеддинг (Voyage) →
               upsert в Postgres под ref="pr:N"  (content-hash дедуп)
                  │
──────────────────────────── LangGraph-граф (agent/) ───────────────────────────
3. plan        дифф → review-units (по файлу): {path, node_ids изменённых символов, patch}
                  │  Send fan-out (файлы ревьюятся параллельно)
                  ▼
4. analyze     LLM (OpenRouter) в tool-loop по каждому файлу:
                 • search_code(query)        → Retriever:
                        embed_query (Voyage) → гибрид-поиск по (base \ changed ∪ overlay):
                          pgvector ANN  +  pg_search BM25  → слияние RRF
                        + graph.expand(изменённые символы) → Neo4j callers/callees (impl/тесты при наличии рёбер)
                        + Voyage rerank → top-N  → ContextPack (код с цитатами path:line)
                 • get_related_symbols(node) → связанные символы из графа
               → LLM выдаёт findings (JSON): category, severity, line, message, suggestion, confidence
                  │  (findings аккумулируются со всех файлов)
                  ▼
5. verify      LLM-скептик отсеивает ТОЛЬКО явно ложные (recall-safe: при сомнении оставляет)
               + policy.gate: категория включена? severity ≥ порога? confidence ≥ порога? путь не в ignore?
                  ▼
6. assemble    findings → разделение:
                 • строка попадает в дифф → inline-комментарий (RIGHT/LEFT)
                 • иначе → пункт в сводку (с ссылкой file:line)
               + кап max_comments + идемпотентность по фингерпринту (не дублировать на повторном push)
                  ▼
7. publish     GitHubProvider: один review = сводка + массив inline-комментариев
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

Нужны: Python 3.11–3.13, Docker, ключи Voyage и OpenRouter, GitHub-токен.

```bash
# 1. зависимости и инфраструктура
git clone https://github.com/mimfort/rag_for_git && cd rag_for_git
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker compose up -d                 # Postgres/ParadeDB (:5433) + Neo4j (:7687)

# 2. конфиг
cp .env.example .env                 # заполнить OPENROUTER_API_KEY, VOYAGE_API_KEY, GITHUB_TOKEN
```

Где взять ключи:
- **Voyage** (`VOYAGE_API_KEY`): https://dashboard.voyageai.com/ — есть 200M бесплатных токенов; чтобы снять лимит 3 RPM / 10K TPM, привяжите карту (списания идут только сверх бесплатного пула; auto-recharge можно держать выключенным).
- **OpenRouter** (`OPENROUTER_API_KEY`): https://openrouter.ai/ — выберите любую модель в `OPENROUTER_MODEL`.
- **GitHub** (`GITHUB_TOKEN`): PAT с правами *Pull requests: Read and write* + *Contents: Read* (fine-grained) или scope `repo` (classic). Быстрый вариант для своих репо: `gh auth token`.

## Использование (CLI)

После `pip install -e .` доступна команда `reviewer`:

```bash
# Проиндексировать базу целевой ветки локального клона (вектора + граф).
# Делается один раз и обновляется инкрементально; даёт RAG/графу контекст всего репо.
reviewer index /path/to/repo --ref main

# Диагностический гибрид-поиск по базе (проверить, что индекс работает).
reviewer search "token verification"

# Отревьюить PR на GitHub и запостить inline-комментарии + сводку.
reviewer review owner/repo 123
```

Типичный сценарий для другого репозитория:

```bash
git clone https://github.com/ORG/REPO /tmp/REPO
reviewer index /tmp/REPO --ref main        # построить базу+граф
reviewer review ORG/REPO 42                 # ревью PR #42
```

> `review` работает и без предварительного `index` — тогда контекст ограничен диффом и overlay (RAG/граф «тонкие»). Для полноценного анализа влияния на весь репозиторий запустите `index` по целевой ветке.

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

> Агент **только комментирует** — он никогда не меняет и не откатывает код. Предложения приходят текстом, а не GitHub-блоком `suggestion`, чтобы случайный «Apply» не вставил совет как код.

## Конфигурация

Всё ключевое — через `.env` (см. `.env.example` с комментариями). Главное:

| Переменная | Назначение |
|---|---|
| `OPENROUTER_MODEL` | модель LLM (любая на OpenRouter) |
| `OPENROUTER_MODELS_FALLBACK` | CSV запасных моделей |
| `OPENROUTER_MAX_PRICE_PROMPT` / `_COMPLETION` | потолок цены за 1M токенов (USD), жёсткий фильтр провайдеров |
| `OPENROUTER_PROVIDER_SORT` | `price` / `throughput` / `latency` |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | модель Voyage и размерность (= колонке `vector(N)`; смена ⇒ реиндекс) |
| `RERANK_MODEL` | модель реранкера Voyage |
| `REVIEW_SEVERITY_THRESHOLD` | мин. важность: `low/medium/high/critical` |
| `REVIEW_MIN_CONFIDENCE` | отбрасывать findings ниже уверенности (0..1) |
| `REVIEW_MAX_COMMENTS` | кап inline-комментариев |
| `REVIEW_CATEGORIES` | CSV вайтлист категорий (пусто = все) |
| `REVIEW_MAX_TOOL_ITERATIONS` | потолок tool-вызовов агента на файл |
| `PG_DSN`, `NEO4J_URI/USER/PASSWORD`, `GITHUB_TOKEN` | подключения и доступ |

**Политика per-repo.** Файл `.review.yml` в **целевой ветке** репозитория переопределяет env-дефолты (PR не может ослабить собственное ревью):

```yaml
categories: { correctness: true, security: true, performance: true, style: false }
severity_threshold: medium
min_confidence: 0.5
paths: { ignore: ["**/migrations/**", "vendor/**"] }
max_comments: 25
```

## Структура проекта

```
reviewer/
  config/      Settings (pydantic-settings): env → провайдер-блок OpenRouter, пороги ревью
  vcs/         VCSProvider + github.py (httpx) · diff.py (строки, доступные для inline)
  index/       chunker(tree-sitter) · embeddings(Voyage) · reranker · store(pgvector+pg_search/RRF) · freshness
  graph/       builder(tree-sitter call-graph) · scip(парсер SCIP, апгрейд точности) · store(Neo4j)
  retrieval/   Retriever: гибрид + graph-expansion + rerank → ContextPack
  llm/         OpenRouterProvider (extra_body: provider/max_price/models) · BudgetTracker
  tools/       инструменты агента (search_code, get_related_symbols)
  agent/       state · nodes · graph (LangGraph) · analyzer (LLM analyze/verify) · prompts
  policy/      ReviewPolicy: env-дефолты + .review.yml + гейтинг
  entrypoints/ cli.py (index / search / review)
  app.py       сборка зависимостей из Settings
docker-compose.yml   ParadeDB (pgvector+pg_search) + Neo4j
```

## Тесты

```bash
.venv/bin/pytest -q                 # unit (быстрые, на фейках; внешние API не дёргают)
.venv/bin/pytest -m integration     # integration: нужны поднятые Postgres/Neo4j + ключ Voyage
```

Внешние сервисы изолированы за интерфейсами и мокаются в unit-тестах; реальные вызовы — только в integration/E2E.

## Ограничения и заметки

- **v1 — только Python** (чанкер и граф). Другие языки — за тем же интерфейсом `GraphIndexer`/чанкера.
- **Граф v1** строится tree-sitter-резолвером по имени вызова (быстро, без внешних тулов). Точный кросс-файловый граф через `scip-python` — заложен (`graph/scip.py`), подключается как апгрейд.
- **Voyage free tier** = 3 RPM / 10K TPM: полная индексация большого репо требует привязанной карты (бесплатные 200M токенов сохраняются) либо медленной инкрементальной индексации; в коде есть retry/backoff.
- **VCS** — пока GitHub; GitLab/др. добавляются реализацией `VCSProvider`.
- **Запуск** — пока CLI; webhook-сервис добавляется как точка входа (ядро уже библиотека).
