# Brief — PRI-202 Конфигурируемые лимиты контекста + интерактивное расширение
https://ru.yougile.com/team/686c049c8af8/#PRI-202

## Task
- **Ключ/заголовок:** PRI-202 (store: ID-202), статус `Бэклог` — «Конфигурируемые лимиты контекста + интерактивное расширение».
- **Реальная цель (формулировка юзера, важнее предложенного решения):** лимиты retrieval-тулов захардкожены и едины для всех репо/тасок, поэтому релевантный контекст **молча отсекается** до того, как LLM его увидит. Нужна реализация, при которой релевантное попадает в бриф **по максимуму** и **адаптивно** к плотности репо и размаху таски — чтобы solve-task строил полноценные брифы. Предложенное в тикете решение (3 части) — **не обязательно**; ищем лучший подход.
- **Захардкоженные точки (из тикета):** `search_codebase top_k=10`, `search_tasks top_k=5`, `related_symbols hops=1`, `callers _CAP=25`; плюс brief-caps в SKILL.md (≤5/≤3/≤3 — **вне скоупа**, отдельная таска).
- **Предложение тикета (3 части, опционально):** (1) секция `context_limits` в `.review.yml`+`ReviewPolicy`; (2) мета-конверт в ответах тулов (`total_found`/`beyond_cap`/`score_range`/`top_outliers`/`by_category`) на **Voyage-реранкер-скорах**, не RRF; (3) интерактивный interrupt — LLM предлагает юзеру расширить выдачу по категориям/числу.
- **Acceptance (8 критериев):** конфиг-секция с обратной совместимостью дефолтов; тулы читают top_k из конфига; `search_codebase` **всегда** прогоняет реранкер и возвращает его скоры; мета-конверт; `related_symbols_hops`/`callers_topk` из конфига; SKILL.md (solve-task+ask) учит interrupt при `beyond_cap>0`; все старые тесты зелёные; новые unit-тесты (парсинг конфига / мета-конверт / by_category / интеграция hops+callers_topk).
- **Скоуп:** только solve-task/ask; PR-review (overlay-сессия) — потом.

## Related work
- **ID-167 — сводки подсистем: per-repo top-k при масштабе** (`summary_topk_threshold` в `.review.yml`). Точный **прецедент паттерна** для Части 1: env-дефолт → override из `.review.yml` ветки, резолв server-side. Мимикрировать этот контракт. (policy.py:23/45/94, service.py:`_resolve_summary_topk_threshold`).
- **ID-178 — reranker fallback: сохранять graph-expanded items при срезе [:top_k].** Та же зона, что Часть 2 правит (rerank/срез в `retriever.py`); полезно как уже-обдуманное поведение усечения.
- **ID-146 — solve-task: спека brief + жёсткий relevance-фильтр (лимиты top-N, drop по порогу).** Установил нынешние brief-caps и фильтр, которые тикет хочет сделать адаптивными; контекст для развязки «retrieval-cap vs brief-cap».
- (dropped 4: ID-150 two-pass hybrid_search — смежная rerank-оптимизация, другой механизм; ID-138 output dedup/headers — смежная форма выдачи, уже смержено; ID-133 GitLab-креды в .review.yml — другая секция конфига; ID-170 project-scope — другой механизм.)

## Subsystems
- `reviewer/retrieval` — `Retriever`: гибрид (RRF) + graph-expansion + Voyage-реранк; `search_base` — base-only путь для solve-task. **Сердце адаптивности.**
- `reviewer/index` — `reranker.py` (VoyageReranker), `store.py` (hybrid_search RRF/top_k), `embeddings` (Voyage). Источник скоров.
- `reviewer/policy` — `ReviewPolicy`: парсинг `.review.yml` + env-дефолты; куда садится `context_limits`.
- `reviewer/mcp` — `MCPReviewService`: session-less тулы `search_codebase`/`search_tasks` (возвращают **форматированную строку**) + резолв `.review.yml` ветки.
- `reviewer/tools` — `graph_format.py` (рендер соседей, `_CAP=25`); `reviewer/tasks` — `TaskStore.search` (search_tasks top_k).

## Relevant code
- `reviewer/retrieval/retriever.py:106-148` — `search_base`: hops захардкожен `hops=1` (:128), реранк **условный** `if reranker is None or len<=3 or (len<=top_k and not graph_new)` (:141), срез `items[:top_k]` (:142/:147). Здесь живут AC 3/5 и любая «адаптивная отсечка по cliff». Тот же rerank-гейтинг в `retrieve()` (:100-104) — PR-путь, не трогать в этом скоупе.
- `reviewer/policy/policy.py:27-99` — `ReviewPolicy.from_yaml`/`from_settings`/`load`: добавить `context_limits` ровно как `summary_topk_threshold` (поле :23, парс :45/:94). AC 1.
- `reviewer/mcp/service.py:395-416` (`search_codebase`) + `:256-259` (`search_tasks`) — **возвращают строку** `pack.as_context(line_numbers=True)`; мета-конверт (AC 4) ломает форму выдачи. **Плумбинг-дыра:** эти тулы **не грузят policy** → лимиты неоткуда взять; паттерн чтения `.review.yml` ветки server-side — `_resolve_summary_depth` (service.py:331).
- `reviewer/index/reranker.py:14-20` — `VoyageReranker.rerank` **отбрасывает** `res.relevance_score` (возвращает только переупорядоченные items). Для `score_range`/`top_outliers`/cliff-детекции скоры надо протащить наружу. AC 3/4.
- `reviewer/tools/graph_format.py:9,43,67` — `_CAP=25` хардкод (срез `[:_CAP]` :43, хвост :67), используется `related_symbols`/`find_callers`. AC 6.

## Test exemplars
- `tests/retrieval/test_search_base.py:98` — `test_search_base_reranks_when_many_hits...`: `_FakeStore`/`_FakeGraph`/`_FakeReranker` с `.calls`-трекингом — готовый паттерн для AC 3 («всегда реранк») и новых тестов мета-конверта.
- `tests/retrieval/test_retriever.py:182,88,106` — rerank skip/call-логика (гейтинг, который правишь): фиксирует, когда Voyage **не** дёргается — береги обратную совместимость TPM.
- `tests/policy/test_policy.py` — `.review.yml` переопределяет env-дефолты (покрывает `summary_cluster_depth`/`summary_topk_threshold` с per-prefix override) — образец для парсинга `context_limits` (AC 1/9).

## Constraints / open questions
- **[design] Чем реально максимизировать охват — главный вопрос для brainstorming.** Тикет даёт 3 рычага (config / мета-конверт / interrupt), но цель юзера — «релевантное по максимуму, адаптивно». Рассмотреть **авто-отсечку по cliff реранкера** (relative threshold: брать, пока скор в пределах X% от топа, останавливаться на обрыве) — это даёт «адаптивный top_k» без конфига и без переспросов, и точнее ловит «разным репо/таскам нужно разное N», чем статический per-repo top_k. Возможна композиция: cliff-cut по умолчанию + interrupt только когда хвост за cliff всё ещё высокоскоровый.
- **[scoring] RRF не разделяет сигнал/шум** (плоская кривая ≈0.0091–0.0164, без cliff'ов) — на нём адаптивная отсечка невозможна; нужны реранкер-скоры. Но `rerank` их сейчас теряет (reranker.py:20) → протащить `relevance_score`.
- **[cost] «Всегда реранк» (AC 3) бьёт по Voyage free tier (3 RPM / 10K TPM).** Сейчас реранк — только при переполнении (экономия). Каждый solve-task-поиск с реранком = доп. Voyage-вызов. Взвесить: реранкать ограниченный candidate-pool / кэш / реранк только при `beyond_cap>0`.
- **[shape] Слом формы выдачи.** Session-less тулы отдают **строку** (`as_context`). Мета-конверт — это `{results, meta}`. Развилка: (a) перейти на dict/JSON (ломает строковых потребителей, но парсинг чище) vs (b) дописать текстовый `## meta`-блок к строке (без слома схемы, LLM парсит прозу). Решение задевает все скиллы-потребители.
- **[plumbing] Где читать конфиг.** `search_codebase`/`search_tasks` сейчас не видят policy. Варианты: (a) читать `.review.yml` ветки server-side (паттерн `_resolve_summary_depth`, как все per-repo настройки по конвенции CLAUDE.md) vs (b) скилл (он уже читает `.review.yml`) передаёт лимиты аргументами тула. Рекомендация — server-side, ради единообразия с `summary_*`.
- **[scope-tension] retrieval-cap vs brief-cap.** Адаптивный retrieval бесполезен, если SKILL.md дальше жёстко режет ≤5 символов. Тикет выносит brief-caps из скоупа (ID-146), но цель «полноценные брифы» этого требует — решить на brainstorming, тянуть ли brief-caps в эту же работу или строго держать границу.
- **[scope] interrupt только в solve-task/ask** — требует паузы-вопроса (AskUserQuestion-стиль); в fan-out PR-сабагентах не работает (поэтому PR-review вне скоупа).
- Индекс свежий (`drift==0` на `dev`), корпус задач прогрет (93), сводки построены — приоры полные.

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 41.2K · out 36.2K · cache-write 417.4K · cache-read 1.8M
Всего: 2.3M токенов
