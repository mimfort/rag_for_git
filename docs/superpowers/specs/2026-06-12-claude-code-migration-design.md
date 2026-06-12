# Дизайн: миграция ревью-агента на Claude Code (скиллы + MCP)

Дата: 2026-06-12
Статус: одобрен (брейншторм с пользователем)

## Цель

Полностью заменить LLM-слой на OpenRouter (LangGraph analyze/verify/synthesize) на **Claude Code**:
человек в сессии Claude Code говорит «заревьюй PR» (или дёргает скилл), и ревью-пайплайн
выполняется мозгом Claude по подписке, без $/PR за OpenRouter. Python-инфраструктура
(RAG, граф кода, Voyage, GitHub) сохраняется и оборачивается в MCP-сервер.
Дальше контур наращивается задачным контекстом: чтение задач с доски через MCP,
граф и векторный поиск по задачам, скилл решения задач.

## Ключевые решения (зафиксированы с пользователем)

| Решение | Выбор |
|---|---|
| LLM-слой | Полная замена на Claude Code; OpenRouter выпиливается после эвал-гейта |
| Граница Python↔Claude | «Толстый MCP, тонкий скилл»: инварианты и публикация — детерминированный Python, анализ и оркестрация сабагентов — скиллы |
| Доска задач | Абстракция `TaskProvider` через конфиг; эталонная реализация — Jira (официальный Atlassian MCP) |
| Порядок фаз | (1) ревью-скилл + MCP-сервер → (2) контекст задачи из Jira → (3) граф+RAG по задачам → (4) скилл решения задач |
| Наблюдаемость | Сохраняется: `publish_review` пишет `review_runs`/`review_findings` в ту же БД; per-call трейс и OpenRouter-стоимость уходят |
| Язык промптов | Скиллы и промпты — английский |
| Язык выдачи | Настраиваемый: `output_language` в `.review.yml`, дефолт `ru` |

## Поставка

Claude Code **плагин `rag-reviewer`** в этом репозитории:

- **MCP-сервер `reviewer-mcp`** — stdio, FastMCP поверх существующего
  `build_components(settings)`; конфиг из того же `.env`. Инвариант single-repo
  (один инстанс БД на репозиторий) не меняется.
- **Скиллы**: `/review-pr`, `/solve-task` (фаза 4), адаптированные
  `performance-review` и `maintainability-review` (из заготовок пользователя;
  Codex-формат `::code-comment` заменяется нашей findings-схемой). Они вызываются
  и самостоятельно, и как измерения внутри `/review-pr`.

Headless/CI-режим: `claude -p "/review-pr owner/repo#123"`.

## Что остаётся / что выпиливается

**Остаётся без изменений**: `index/` (chunker, эмбеддинги, store), `graph/`
(SCIP/tree-sitter, Neo4j), `retrieval/` (гибрид RRF + rerank), `vcs/` (GitHub),
`policy/`, `web/` (админка), CLI `index`/`search`/`check`/`serve`.

**Выпиливается после эвал-гейта**: `llm/openrouter.py`, `llm/budget.py`,
LangGraph (`agent/graph.py`, узлы analyze/verify в `nodes.py`), `agent/analyzer.py`,
русские промпты `agent/prompts.py`, настройки `OPENROUTER_*`, команда
`reviewer review`. Логика assemble (commentable lines, suggestion-инварианты,
fingerprint, кап) переезжает из `nodes.py` в реализацию `publish_review`.
Кост-аналитика в админке упрощается (для новых прогонов стоимости нет).

## Фаза 1: MCP-сервер + скилл /review-pr

### Тулы MCP-сервера

| Тул | Контракт |
|---|---|
| `prepare_review(repo, pr)` | ingest PR (base/head sha, файлы+патчи) → досинхронизация base-индекса по `index_meta` (GitHub compare) → overlay изменённых `.py` в `ref="pr:N"` → `.review.yml` из base-ветки. Возвращает метаданные PR, policy-сводку (категории, severity-порог, кап, `output_language`) и юниты ревью: path, patch, commentable lines. Начинает с удаления старого overlay этого PR (self-healing). |
| `search_code(query, top_k)` | существующий тул: гибрид RRF (base∪overlay) + graph-expansion + Voyage rerank → сниппеты с `node_id`/path/строками |
| `get_related_symbols(node_id)` | соседи по графу Neo4j (CALLS/IMPLEMENTS) |
| `publish_review(pr, summary, findings[], dry_run)` | детерминированный хвост: dedup → policy gate → inline/сводка по commentable lines → suggestion-инварианты `_can_apply` → fingerprint-идемпотентность → кап → один GitHub review. Пишет `review_runs`/`review_findings` (fail-soft), чистит overlay. Возвращает отчёт: что запостили / что срезал gate / что ушло в сводку. |

### Поток скилла /review-pr (SKILL.md на английском)

1. Распарсить ссылку/номер PR из аргумента.
2. `prepare_review` → юниты + policy.
3. **Фан-аут сабагентов по файлам** (аналог `Send` в LangGraph): каждый получает
   дифф своего файла + правило прицельного поиска (перенос из текущего analyze),
   ведёт tool-loop через `search_code`/`get_related_symbols`, возвращает findings
   в текущей JSON-схеме (category, severity, confidence, path, line, title, body,
   suggestion). Батчинг при 30+ файлах — на усмотрение скилла.
4. **Параллельные сабагенты-измерения на весь дифф**: perf и maintainability
   по методологии соответствующих скиллов; findings — в общий формат.
5. **Verify-сабагент**: скептик с контекстом, отсекает галлюцинации file:line.
   Recall-safe: сомневаешься → оставь (текущая семантика fail-open).
6. `publish_review` (или `dry_run`) → отчёт пользователю.

Findings-схема и verify-критерии — перевод текущих `prompts.py` на английский.

## Фаза 2: контекст задачи в ревью

Поиск ключа задачи: аргумент пользователя → regex по title/body PR
(`key_pattern`, URL трекера). Конфиг доски в `.review.yml`:

```yaml
task_board:
  type: jira          # эталон; абстракция = тип + имя MCP-сервера
  mcp: atlassian      # какие MCP-тулы скилл зовёт для чтения задач
  key_pattern: "[A-Z]+-\\d+"
```

Скилл читает задачу через MCP доски (summary, description, acceptance criteria,
links) и передаёт ревью-сабагентам: «PR заявляет реализацию задачи X — проверь
соответствие». Новая категория findings — `requirements`. Задачи нет / доска
не настроена → ревью работает как в фазе 1, без деградации.

## Фаза 3: граф и RAG по задачам

- **Neo4j**: узлы `(:Task {key, title, status, url})`, рёбра
  `TASK_LINK {type: blocks|relates|duplicates|parent}` (issue links доски),
  `(:Task)-[:IMPLEMENTED_BY]->(:PR)`, `(:PR)-[:TOUCHES]->` код-узлы `path#fqn` —
  задачи сшиваются с кодом через существующий ключ `node_id`.
- **Postgres**: эмбеддинги задач (title+description) отдельным kind/ref;
  тот же гибрид-поиск.
- Новые MCP-тулы: `index_task(task_json)` (скилл достал задачу из MCP доски,
  нормализовал → вектора+граф), `search_tasks(query)` (похожие по смыслу),
  `get_task_context(key)` (граф-обход: связанные задачи → их PR → затронутый код).
- Bulk-синк всей доски — открытый вопрос спеки фазы 3 (кандидат: скилл
  `/sync-tasks`, итерирующий доску через MCP).

## Фаза 4: скилл /solve-task

`/solve-task PROJ-123`: читает задачу с доски → `get_task_context` +
`search_tasks` (напрямую связанные и семантически похожие задачи, их PR и код) →
`search_code` по формулировке → сводит только релевантное в бриф → дальше обычная
разработка в Claude Code (план → реализация → тесты). Скилл дисциплинирует сбор
контекста, не заменяет разработку.

## Обработка ошибок и деградация

- MCP-тулы возвращают структурированные ошибки с подсказкой к действию
  (`prepare_review` при недоступном Postgres → «docker compose up -d / reviewer check»).
- Neo4j недоступен → `get_related_symbols`/`get_task_context` отдают пусто
  с warning; ревью продолжается без графа.
- Verify recall-safe: сабагент упал / вердикт не разобран → findings проходят.
- `publish_review` валидирует строки против commentable lines: мимо диффа →
  в сводку, находка не теряется. Повторный прогон не плодит дубликаты (fingerprint).
- Сбой одного сабагента-ревьюера не валит прогон: скилл продолжает с остальными
  файлами и помечает пропущенные в сводке.
- Overlay: чистится и в начале `prepare_review`, и в конце `publish_review`
  (двойная страховка вместо `finally` в CLI).

## Тестирование и эвал-гейт

- **Unit**: MCP-тулы — тонкие обёртки над оттестированными компонентами;
  новые тесты на `prepare_review` (сборка юнитов, commentable lines) и
  `publish_review` (gate, dedup, suggestion, идемпотентность) — частично
  переезжают из тестов `nodes.py`.
- **Integration**: MCP-сервер через stdio-клиент на поднятых Postgres/Neo4j
  (маркер `integration`).
- **Эвал-гейт миграции**: dry-run `/review-pr` на эталонных PR из замера D2,
  сравнение полноты/точности с OpenRouter-бейзлайном. Старый путь удаляется
  только после прохождения эвала — им фаза 1 завершается, а не начинается.

## Реализация

План и спеки — Fable; код — Opus/Sonnet-сабагенты по плану. Каждая фаза —
отдельная спека → план → PR с рабочим результатом.
