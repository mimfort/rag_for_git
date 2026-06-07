# Дизайн: агент ревью merge/pull request'ов (RAG + граф кода + LLM)

**Дата:** 2026-06-07
**Статус:** дизайн утверждён, готов к плану реализации
**Язык целевого репозитория v1:** Python

## 1. Цель

Агент, который автоматически ревьюит изменения в pull/merge request'ах. На событие «появился/обновился PR» агент собирает релевантный контекст (RAG по всему репозиторию + граф кода), прогоняет его через LLM из OpenRouter с возможностью вызывать инструменты (agentic RAG), и постит результат обратно в систему контроля версий: inline-комментарии на строки диффа + сводный комментарий.

Архитектура должна быть переносимой между VCS (GitHub в v1, GitLab и прочие — через общий интерфейс) и провайдерами моделей.

## 2. Зафиксированные решения

| Решение | Выбор | Почему |
|---|---|---|
| Runtime/триггер | Ядро-библиотека + точки входа: CLI сейчас, webhook-сервис позже | Максимум гибкости; одно ядро под все сценарии |
| VCS v1 | GitHub (за интерфейсом `VCSProvider`) | Под GitLab/др. абстракция заложена с самого начала |
| Вывод ревью | Inline-комментарии на строки диффа + сводный комментарий | Самый полезный формат для людей |
| Охват ревью | Настраивается политиками per-repo | Гибко, без жёсткого зашивания категорий |
| Свежесть RAG | Стабильная база + content-hash дедуп + overlay на PR | Произвольный ретрив по всему репо + дешёвый по латентности инкремент |
| Граф кода (рёбра) | tree-sitter + резолвер в v1, `scip-python` как апгрейд точности | Точность кросс-файловых рёбер для анализа «что сломается» |
| Хранилище графа | Neo4j | Удобные обходы/визуализация; есть опыт |
| Хранилище вектор+текст | Postgres + pgvector (HNSW) + ParadeDB `pg_search` (BM25) | Один контейнер закрывает гибридный поиск |
| Эмбеддинги/реранк | Voyage (`voyage-code-3`, `rerank-2.5`), модели из env | Квота не лимитирует; code-специализированные модели |
| LLM | OpenRouter (OpenAI-совместимый), модель из env | Гибкий выбор модели/провайдера |
| Контроль стоимости | Потолок цены OpenRouter (USD/1M, input+output) + app-level бюджет на ревью | Два независимых механизма: цена провайдера и расход на один MR |
| Оркестрация | LangGraph, map-reduce по diff с фазой verify | Знакомо; адверсариальная проверка повышает сигнал |

## 3. Стратегия свежести RAG (ключевое)

У нас фактически два корпуса: *стабильная база* (целевая ветка, меняется редко) и *дельта PR* (изменённые файлы, меняются с каждым push'ем). Полный реиндекс на каждый push недопустим по латентности на большом репо.

### 3.1 Персистентный индекс целевой ветки

Каждый файл парсится tree-sitter'ом и режется **по символам** (функция/метод/класс — не фиксированными окнами строк). Чанк несёт метаданные — ключ джойна с графом:

```
chunk {
  id, content_hash,              // sha256 нормализованного тела чанка
  embedding vector(N),          // Voyage; N = EMBEDDING_DIM
  path, lang, symbol_fqn, kind, // напр. "service/user.py :: UserService.create"
  start_line, end_line,
  tsvector                      // для BM25
}
```

`content_hash` — уникальный ключ; эмбеддинг привязан к хэшу контента, а не к (path, sha) → один и тот же код эмбедится один раз.

### 3.2 Инкрементальное обновление базы

При движении целевой ветки (merge; в CLI-режиме — лениво при старте ревью):

```
changed = git diff last_indexed..target_HEAD --name-only
для каждого changed-файла:
   re-parse → новые чанки → hash
   embed ТОЛЬКО хэши, которых нет в store        # дедуп ради латентности
   обновить (path → chunk) маппинг целевой ветки
```

### 3.3 Overlay на PR

```
diff = target_HEAD..PR_head
overlay = чанки только изменённых/добавленных файлов на PR head
          (дедуп по хэшу: re-push неизменённого файла = 0 работы)
```

### 3.4 Слияние на запросе

```
retrieval(query, PR) =
    (ANN + BM25 по base)  WHERE path ∉ changed_paths   // спрятать устаревшие версии
  ∪ (ANN + BM25 по overlay)                            // новые версии изменённых файлов
```

Изменённые файлы представлены **новым** кодом; остальной репозиторий — стабильной базой. Удалённые файлы исключаются, новые — только в overlay, переименования = delete+add.

## 4. Pipeline ретрива

```
diff PR ─┬─► под-запросы (изменённые символы, хунки, заголовок/описание PR)
         ▼
   HYBRID SEARCH по (base\changed ∪ overlay)
     • pgvector ANN (Voyage)   • BM25 (pg_search)   • слияние через RRF
         ▼
   GRAPH EXPANSION (Neo4j, 1–2 хопа)
     изменённые символы → callers / callees / implementers / importers / тесты
     добавляем их чанки в кандидаты
         ▼
   Voyage RERANK (модель из env) → top-N
         ▼
   сборка контекста под токен-бюджет (+ цитаты path:line)
```

Семантический поиск ловит «похожую логику/дубликаты где-то ещё»; граф добавляет структурно связанное («что сломается»); реранкер отсеивает шум перед дорогой LLM.

## 5. Граф кода

- **Узлы:** `File`, `Symbol` (Function/Method/Class/Module). Ключ узла = `path#fqn`, общий с чанками.
- **Рёбра:** `CALLS`, `IMPORTS`, `DEFINES`, `IMPLEMENTS`/`EXTENDS`, `REFERENCES`, `TESTED_BY`.
- **Построение:** tree-sitter для узлов и чанкинга всегда; рёбра — за интерфейсом `GraphIndexer`. v1: tree-sitter + import-aware резолвер. Апгрейд точности: `scip-python` для основного языка.
- **Свежесть:** тем же паттерном, что и вектора — база графа обновляется при merge; дельта PR патчится файл-локально по изменённым символам (overlay-граф). Входящие рёбра к изменённому символу стабильны, пока не меняются имя/сигнатура.

## 6. OpenRouter: модель, потолок цены, роутинг

OpenRouter OpenAI-совместим → используем клиент с `base_url=https://openrouter.ai/api/v1`, специфика в `extra_body`. Потолок цены и выбор провайдера применяются **только к OpenRouter-провайдеру**; другие LLM-провайдеры за интерфейсом `LLMProvider` эти knobs игнорируют.

### 6.1 Env

```bash
# --- LLM через OpenRouter ---
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=anthropic/claude-sonnet-4.5     # любая модель OpenRouter
OPENROUTER_MODELS_FALLBACK=...,...               # опц. -> top-level "models"
OPENROUTER_MAX_PRICE_PROMPT=3.0                  # USD за 1M ВХОДНЫХ токенов  -> provider.max_price.prompt
OPENROUTER_MAX_PRICE_COMPLETION=15.0             # USD за 1M ВЫХОДНЫХ токенов -> provider.max_price.completion
OPENROUTER_PROVIDER_SORT=price                   # price | throughput | latency -> provider.sort
OPENROUTER_PROVIDER_ORDER=                        # опц. явный порядок -> provider.order
OPENROUTER_PROVIDER_ONLY=                         # опц. вайтлист -> provider.only
OPENROUTER_PROVIDER_IGNORE=                       # опц. блэклист -> provider.ignore
OPENROUTER_ALLOW_FALLBACKS=true                  # -> provider.allow_fallbacks
OPENROUTER_REQUIRE_PARAMETERS=true               # только провайдеры с нашими параметрами (tools)
OPENROUTER_DATA_COLLECTION=deny                  # приватность кода -> provider.data_collection
OPENROUTER_MIN_THROUGHPUT=                        # опц. -> provider.preferred_min_throughput
OPENROUTER_MAX_LATENCY=                           # опц. -> provider.preferred_max_latency

# --- App-level бюджет на одно ревью (поверх потолка цены) ---
REVIEW_MAX_TOKENS=                               # потолок суммарных токенов на MR
REVIEW_MAX_TOOL_ITERATIONS=                      # потолок tool-вызовов агента на MR

# --- Voyage (эмбеддинги + реранк) ---
VOYAGE_API_KEY=...
EMBEDDING_MODEL=voyage-code-3                     # code-специализированный
EMBEDDING_DIM=1024                               # ДОЛЖЕН совпадать с колонкой vector(N)
RERANK_MODEL=rerank-2.5
```

### 6.2 Тело запроса

```json
{
  "model": "anthropic/claude-sonnet-4.5",
  "models": ["...fallback..."],
  "messages": [],
  "tools": [],
  "provider": {
    "sort": "price",
    "max_price": { "prompt": 3.0, "completion": 15.0 },
    "allow_fallbacks": true,
    "require_parameters": true,
    "data_collection": "deny"
  }
}
```

### 6.3 Подтверждённые факты (дока OpenRouter)

- `max_price.prompt` / `max_price.completion` — **USD за 1M токенов**; это *жёсткий фильтр*, провайдеры дороже отсекаются. Если под потолок никто не подходит — запрос падает; ловим и даём внятную ошибку.
- `sort` принимает ровно `price` / `throughput` / `latency`; SLO — `preferred_min_throughput`, `preferred_max_latency`.
- `require_parameters: true` — критично: агент завязан на tool-calling.
- Смена `EMBEDDING_MODEL`/`EMBEDDING_DIM` = реиндекс (колонка `vector(N)` фиксирована).

Источники: [Provider Routing](https://openrouter.ai/docs/guides/routing/provider-selection), [API Parameters](https://openrouter.ai/docs/api/reference/parameters), [Nitro/Floor shortcuts](https://openrouter.ai/announcements/introducing-nitro-and-floor-price-shortcuts).

## 7. Оркестрация агента (LangGraph)

Граф состояний, map-reduce по diff (большие PR параллелятся):

```
ingest ─► ensure_index ─► plan ─► [ retrieve ─► analyze ─► verify ]·(per unit) ─► assemble ─► publish
```

- **ingest** — PR-метаданные, diff `target_HEAD..PR_head`, изменённые файлы, загрузка политики.
- **ensure_index** — лениво доводим базу (вектор+граф) до `target_HEAD`; строим PR-overlay (вектор+граф-дельта).
- **plan** — режем diff на review-units (по файлу/кластеру хунков), выделяем изменённые символы.
- **retrieve** (per unit) — гибрид + graph-expansion + rerank → context pack.
- **analyze** (per unit) — LLM с тулзами; дотягивает код тулзами; выдаёт структурные findings (category, severity, file, line, message, suggestion, confidence). Категории гейтятся политикой.
- **verify** — адверсариальный самоперепроверочный проход: воспроизводится ли замечание? цитируемый код действительно такой? проходит ли порог severity и политику? → отсев галлюцинаций/ложных срабатываний.
- **assemble** — дедуп, ранжирование, кап по `max_comments`, разделение на inline vs summary.
- **publish** — через `VCSProvider`, идемпотентно.

## 8. VCS-провайдер (GitHub v1)

- **Auth:** PAT/`gh`-токен для CLI v1; `VCSProvider` готов под GitHub App для webhook-фазы.
- **Постинг:** Reviews API — один review с `body` (сводка) + `comments:[{path, line, side, body}]`.
- **Маппинг inline:** GitHub разрешает inline-комментарий **только на строках, попавших в diff**. Findings на изменённых строках → inline; на неизменённом контексте → в сводку с ссылкой `file:line`. Инвариант зашит в `assemble`.
- **Идемпотентность (re-push):** каждый комментарий помечается скрытым фингерпринтом `<!-- ai-review:hash(file+line+rule+msg) -->`. На повторный push не дублируем выставленное, добавляем только новое, устаревшее помечаем resolved.

Интерфейс `VCSProvider` (минимум): `get_pr()`, `get_diff()`, `get_file(path, ref)`, `list_existing_review_comments()`, `publish_review(summary, inline_comments)`.

## 9. Политика per-repo (`.review.yml`)

```yaml
categories: { correctness: true, security: true, performance: true, style: false }
severity_threshold: medium      # ниже — отбрасываем
paths: { ignore: ["**/migrations/**", "vendor/**"] }
max_comments: 25
```

Грузим из **целевой ветки** (а не из PR head) — чтобы PR не мог ослабить собственное ревью. `analyze` включает только разрешённые категории.

## 10. Разбивка на модули

```
reviewer/
  config/        env→Settings (pydantic-settings)
  vcs/           VCSProvider + github/         (gitlab/ позже)
  index/         chunker(tree-sitter) · embeddings(Voyage) · store(pgvector+pg_search/RRF) · freshness(hash+overlay)
  graph/         builder(tree-sitter resolver + scip-python) · store(neo4j, expansion)
  retrieval/     гибрид + graph-expansion + Voyage rerank
  llm/           LLMProvider + openrouter/    (price/routing/бюджет)
  tools/         search_code, get_symbol, get_callers/callees, find_tests, git_blame…
  agent/         LangGraph-граф ревью
  policy/        парсинг+гейтинг .review.yml
  entrypoints/   cli/ (сейчас) · webhook/ (позже)
docker/          compose: postgres(pgvector+pg_search) · neo4j · app
```

Принцип: каждый модуль — одна ответственность, общается через явный интерфейс, тестируется изолированно.

## 11. Ошибки и устойчивость

- Сбой индекса/графа → деградируем (ревьюим на доступном контексте, помечаем сниженную уверенность).
- Никто не под потолком цены → внятная ошибка.
- Исчерпан бюджет ревью (`REVIEW_MAX_TOKENS`/`REVIEW_MAX_TOOL_ITERATIONS`) → стоп + частичное ревью с пометкой.
- Внешние вызовы — с ретраями/бэкоффом; постинг идемпотентный.

## 12. Тестирование

- **Unit:** границы чанков (tree-sitter); корректность merge `base\changed ∪ overlay`; RRF; сборка OpenRouter-запроса (env→`provider`); гейтинг политикой; фингерпринт-идемпотентность.
- **Integration:** dockerized Postgres+Neo4j; индексируем fixture-реп; проверяем релевантность ретрива и graph-expansion.
- **E2E:** fixture-PR с известным багом → нужный finding + формат постинга (mock VCS/LLM или записанные ответы).
- **Eval-харнес:** набор PR с известными проблемами → precision/recall замечаний (главная метрика качества).

## 13. Вне области v1 (заложено абстракциями)

- GitLab и прочие VCS (через `VCSProvider`).
- Webhook-сервис как точка входа (ядро уже библиотека).
- Прямые провайдеры моделей помимо OpenRouter (через `LLMProvider`).
- SCIP-инкрементальность и мультиязычный граф.

## 14. Открытые вопросы для фазы планирования

- Точная схема таблиц Postgres (chunks, snapshot/branch-маппинг) и индексы HNSW/`pg_search`.
- Стратегия чанкинга крупных функций (под-разбиение по токен-лимиту эмбеддера).
- Состав eval-набора PR для метрик качества.
- Формат фингерпринта комментария и политика «resolved» устаревших.
