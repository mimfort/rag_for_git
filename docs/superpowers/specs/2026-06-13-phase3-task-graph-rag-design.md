# Спека Фазы 3: граф и RAG по задачам

Дата: 2026-06-13
Статус: одобрен (брейншторм с пользователем)
Базовый дизайн: `2026-06-12-claude-code-migration-design.md` (раздел «Фаза 3»)
Фундамент: `2026-06-12-phase2-task-context-design.md` (контракт `TaskBrief`, `task_keys`, `task_board`)

## Цель

Задачи доски попадают в **граф** (Neo4j) и **векторный индекс** (Postgres), сшиваясь
с кодом через существующий ключ `node_id = "path#fqn"`. При ревью PR со связанной
задачей агент через `get_task_context` видит **связанные задачи → их PR → затронутый
код**, а через `search_tasks` — **похожие по смыслу** задачи. Этот контекст усиливает
`requirements`-проверку фазы 2. Те же тулы становятся фундаментом скилла `/solve-task`
(фаза 4).

Граф задач наполняется двумя путями:
- **лениво** — скиллы (`review-pr`, позже `solve-task`) индексируют встреченные задачи
  через `index_task`;
- **bulk-прогревом** — тонкий скилл `/sync-tasks` итерирует доску и индексирует всё
  (решает cold-start пустого `search_tasks`).

PR↔задача↔код сшиваются **автоматически на хвосте `publish_review`**: у ревью уже есть
номер PR, `changed_node_ids` и (с фазы 2) primary-ключ задачи.

## Не-цели (вне scope фазы 3)

- Скилл `/solve-task` (фаза 4) — встаёт на тулы этой фазы, но реализуется отдельно.
- Мульти-board одновременно (инвариант single-repo сохраняется: один инстанс БД на репо).
- Авто-постинг результата ревью обратно в доску.
- Рёбра `blocks`/`duplicates` для Yougile — у доски нет источника (см. ниже); они
  опциональны и появляются только там, где доска их отдаёт (Jira issue-links).
- Смешивание эмбеддингов задач с code-`chunks` (отдельная таблица — см. ниже).
- Семантические рёбра «relates» как материализованные связи в графе (близость — query-time).
- Бандлинг board MCP в плагин (как и в фазе 2 — пользователь подключает сам).

## Ключевые решения (зафиксированы с пользователем)

| Вопрос | Решение |
|---|---|
| Канонический ключ `:Task` при двух namespace Yougile | Человечный сквозной код (Yougile `ID-N` / Jira issue key) + `aliases[]` (Yougile `PRI-N`). PR по любому коду резолвится в один узел. |
| Кто наполняет `(:PR)` + рёбра `IMPLEMENTED_BY`/`TOUCHES` | Авто на хвосте `publish_review` (реальная публикация, не dry-run), переиспользуя `changed_node_ids` из сессии. |
| Наполнение корпуса задач | Ленивый `index_task` (ядро) + тонкий идемпотентный `/sync-tasks` (прогрев). |
| Вход `index_task` | Нормализованный `TaskBrief` (нормализует **скилл** по плейбуку; Python на доску не ходит — как в фазе 2). |
| Хранение эмбеддингов задач | Отдельная таблица `tasks` (не code-`chunks`): у задач нет path/symbol/lines/overlay-freshness. Тот же RRF BM25+HNSW. |
| Рёбра задача↔задача | Только из явных board-links (`TaskBrief.links[]`). Yougile: `parent`/`subtask` из `subtasks[]`. Семантика — query-time, не ребро. |
| Эмбеддер задач | Существующий Voyage (`voyage-code-3`, dim 1024). `search_tasks` — чистый RRF без реранка (бережём Voyage TPM; реранк ~5 задач не стоит вызова). Новой модели не вводим. |
| `:Task.url` | Заполняется из `task_board.url_template` (web-facing код — Yougile project code `PRI-N`). Дефолт `null`. |
| Деградация | fail-open, как фазы 1/2: Neo4j/доска недоступны → пустой результат + warning, прогон не падает. |

## Контракт `TaskBrief` (расширение фазы 2)

Фаза 2 определила board-agnostic бриф; фаза 3 **добавляет** опц. `aliases[]` и начинает
**использовать** `links[]`. Нормализует по-прежнему скилл (Python на доску не ходит).

```
TaskBrief:
  key:         "ID-12"          # канонический: сквозной код доски (Yougile ID-N / Jira key)
  aliases:     ["PRI-4"]        # NEW: прочие коды той же задачи (Yougile project code); опц., дефолт []
  title:       "<task title>"
  description: "<task description / requirements text>"
  criteria:    ["<acceptance criterion>", ...]   # best-effort, может быть []
  status:      "<status name>"
  url:         "<task link>"     # из url_template; null если шаблона нет
  links:       [{type, key, title}, ...]   # NEW-в-использовании: board-links; Yougile: subtasks → type="subtask"
```

**Yougile-плейбук (правки фазы 3):**
- `key` ← `idTaskCommon` (`ID-N`, сквозной по компании — глобально уникальный, лучший канон).
- `aliases` ← `[idTaskProject]` (`PRI-N`, по проекту).
- `links` ← по каждому UUID из `subtasks[]`: `get_task` → `{type:"subtask", key, title}`
  (best-effort; сбой резолва подзадачи не валит бриф — частичный список валиден).
- `url` ← `task_board.url_template` с подстановкой **проектного кода** (`PRI-N` —
  именно он в web-фрагменте `…/team/<teamId>/#PRI-4`), не канонического `ID-N`.

**Jira-плейбук:** `key` ← issue key; `aliases` ← `[]`; `links` ← issue-links с их типами
(`blocks`/`relates`/`duplicates`); `url` ← issue self-link.

**Извлечение ключей из вставленных ссылок.** Подтверждено на `task_keys.py`: дефолтный
`key_pattern [A-Z]+-\d+` достаёт код прямо из web-ссылки (`…/#PRI-4` → `PRI-4`), а хекс
team-id ложных матчей не даёт. **Python-извлечение НЕ меняется** — ссылка в title/body
ловится как есть. Ссылка обычно несёт проектный код `PRI-N` → он станет `primary`;
канонизация в `ID-N` + `aliases` (на стороне скилла) сшивает это в один узел.

## Хранилище 1: Postgres — таблица `tasks`

Новая таблица (отдельно от `chunks`):

```sql
CREATE TABLE tasks (
    id           bigserial PRIMARY KEY,        -- key_field для bm25
    key          text NOT NULL UNIQUE,         -- канонический код
    aliases      text[] NOT NULL DEFAULT '{}',
    title        text NOT NULL,
    description  text NOT NULL DEFAULT '',
    status       text,
    url          text,
    content_hash text NOT NULL,                -- sha256 нормализованного (title+description+criteria)
    embedding    vector(1024)
);
CREATE INDEX tasks_bm25 ON tasks
    USING bm25 (id, key, title, description) WITH (key_field='id');
CREATE INDEX tasks_hnsw ON tasks
    USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
```

- **Текст эмбеддинга** = `title` + `\n` + `description` + `\n` + `"\n".join(criteria)`.
- **`content_hash`** = `sha256` нормализованного текста эмбеддинга (rstrip строк + strip).
  При `upsert_task` с тем же `key` и тем же `content_hash` — **embed пропускается**
  (идемпотентность `/sync-tasks` и экономия Voyage TPM). Изменился текст → переэмбед.
- **`search`** = тот же RRF `1/(60+rank)` BM25+ANN, что в `chunks.hybrid_search`, но без
  `ref`-фильтра (задачи не PR-версионируются, base/overlay к ним не применяется).

Размерность вектора и модель — те же, что у кода (`voyage-code-3`, 1024). Задачи —
естественный язык, но единое пространство нужно для сравнения query↔task и task↔task;
`voyage-code-3` справляется, отдельную text-модель не вводим (YAGNI).

## Хранилище 2: Neo4j — узлы и рёбра задач

Узлы и рёбра живут в **той же** Neo4j-инстанции, что код-граф `:Symbol` (рёбра `TOUCHES`
связывают `:PR` с `:Symbol` — это один граф).

**Узлы:**
```
(:Task {
   key,                 // канонический; constraint UNIQUE
   codes: [key, ...aliases],  // для резолва по любому коду: WHERE $k IN t.codes
   title, status, url
})
(:PR {
   id,                  // "owner/repo#N"; constraint UNIQUE
   repo, number, url, sha
})
```
- Constraint uniqueness: `:Task(key)`, `:PR(id)`. Индекс на `:Task(codes)` (Neo4j
  индексирует элементы массива → `$k IN t.codes` использует индекс).

**Рёбра:**
| Ребро | Источник | Семантика |
|---|---|---|
| `(:Task)-[:TASK_LINK {type}]->(:Task)` | `TaskBrief.links[]` | явные board-links; Yougile `type="subtask"`. Несуществующий сосед → MERGE-стаб `:Task{key,title,codes:[key]}`. |
| `(:Task)-[:IMPLEMENTED_BY]->(:PR)` | хвост `publish_review` | задача реализована в PR. |
| `(:PR)-[:TOUCHES]->(:Symbol)` | хвост `publish_review` | `changed_node_ids` ревью; MERGE `:Symbol{id}` (стаб, если узла ещё нет в код-графе). |

Семантическая близость задач — **не ребро**, а query-time через `search_tasks`.

## Новый пакет `reviewer/tasks/`

Изолированный пакет (по образцу `index/` + `graph/`), чтобы task-логика держалась
отдельными малыми единицами:

- **`tasks/store.py` — `TaskStore`** (Postgres `tasks`, тот же пул/DSN, что `ChunkStore`):
  - `upsert_task(row: TaskRow) -> bool` — вставка/обновление; возвращает `embedded`
    (`False`, если `content_hash` совпал и embed пропущен).
  - `search(query_text, query_embedding, top_k) -> list[TaskHit]` — RRF BM25+ANN.
  - `existing_hash(key) -> str | None` — для дедупа до эмбеда.
  - `TaskRow`/`TaskHit` — dataclasses (по образцу `ChunkRow`/`Retrieved`).
- **`tasks/graph.py` — `TaskGraph`** (переиспользует Neo4j-драйвер существующего
  `GraphStore` — один коннект; рёбра `TOUCHES` ссылаются на `:Symbol` того же графа):
  - `upsert_task(key, aliases, title, status, url) -> None`.
  - `upsert_links(key, links: list[dict]) -> int` — `TASK_LINK` + стабы соседей.
  - `link_pr(task_key, pr: PRRef, touched_node_ids: list[str]) -> None` —
    `:PR` + `IMPLEMENTED_BY` + `TOUCHES`.
  - `task_context(key) -> dict` — обход: резолв узла по `codes` → сама задача и её
    `IMPLEMENTED_BY` PR + `TASK_LINK`-соседи и их PR → `TOUCHES` код-узлы.
- **`tasks/service.py` — `TaskService`** (оркестрация поверх store + graph + embedder):
  методы `index_task`, `search_tasks`, `get_task_context`, `link_review`.
  MCP-слой делегирует сюда.

**Проводка.** `Components` (`reviewer/app.py`) получает `task_store`, `task_graph`,
`task_service`; `build_components` их собирает (DSN — общий с `ChunkStore`; Neo4j-драйвер
шарится с `GraphStore`). Схема `tasks`-таблицы добавляется в `schema.sql` (idempotent
`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`, применяется существующим
механизмом инициализации схемы).

## MCP-тулы (+3, репо-глобальные, без сессии)

Новые тулы не привязаны к `(repo, pr)`-сессии (инвариант single-repo: один корпус задач
на инстанс). Регистрируются `@mcp.tool()` в `create_server`, делегируют в `TaskService`.

| Тул | Контракт |
|---|---|
| `index_task(task: dict) -> dict` | принимает нормализованный `TaskBrief` → дедуп по `content_hash` (skip embed если не изменилось) → upsert в `tasks` + upsert `:Task` + `TASK_LINK`. Возврат `{key, embedded: bool, links_upserted: int, warnings: [...]}`. Fail-soft по слоям (Neo4j ↓ → embed всё равно проходит). |
| `search_tasks(query: str, top_k: int = 5) -> str` | гибрид по `tasks` → похожие по смыслу: форматированный список `{key, title, status, score}`. Пусто/Postgres-проблема → `"(похожих задач не найдено)"`. |
| `get_task_context(key: str) -> str` | резолв `:Task` по `key`/alias → сама задача и её `IMPLEMENTED_BY` PR + `TASK_LINK`-соседи (с типом) и их PR → `TOUCHES` код-узлы. Структурированный текст, ограниченный `max_tool_result_chars`. Neo4j ↓ / задача не в графе → `"(контекста задачи в графе нет)"` + warning. |

**`publish_review` получает опц. параметр `task_key: str | None = None`.** При реальной
публикации (не dry-run) и заданном `task_key`:
`task_service.link_review(task_key, pr_ref, changed_node_ids)` → `:PR` + `IMPLEMENTED_BY`
+ `TOUCHES`. `pr_ref`/`changed_node_ids` берутся из `_Session.prepared`. Граф недоступен →
warning в отчёт, ревью публикуется штатно. Скилл передаёт **канонический** ключ (тот же,
что в `index_task`), чтобы `:PR` линковался к каноническому узлу.

## Скиллы

### `review-pr` (правка)

Между «Task context» (фаза 2: построен `TaskBrief`) и «Dimensions»:
1. **Persist:** `index_task(TaskBrief)` — задача попадает в граф+индекс (идемпотентно).
2. **Enrich:** `get_task_context(key)` + `search_tasks(title/description)` → связанные/
   похожие задачи и их PR/код вплетаются в контекст `requirements`-измерения
   (`references/requirements-prompt.md` получает доп. блок «related tasks & their PRs»).
3. **Link:** канонический `task_key` передаётся в `publish_review` для авто-линковки PR.

Деградация: любой сбой (`index_task`/`get_task_context`/`search_tasks` упал, граф/доска
недоступны) → шаг пропускается, в сводке пометка; ревью идёт как фаза 2.

### `/sync-tasks` (новый, тонкий)

Отдельный скилл `plugin/skills/sync-tasks/SKILL.md` (английский). Прогрев корпуса:
- Итерирует доску через board MCP по плейбуку `references/sync-tasks-yougile.md`
  (Yougile: `get_boards` → `get_columns` → задачи колонок; резолв статусов через
  `get_column`).
- По каждой задаче строит `TaskBrief` (та же нормализация, что `task-context-yougile.md`)
  и зовёт `index_task`.
- **Идемпотентно** (дедуп по `content_hash` — повторный прогон дёшев).
- **Rate-limit под Voyage** (3 RPM / 10K TPM): полагается на retry/backoff `index/_retry.py`;
  опц. аргументы `--board <name>` / `--limit <N>` для скоупа; лог прогресса
  («indexed/skipped/failed»).
- fail-open: сбой одной задачи не валит синк.

Первый прогон на большой доске может троттлиться — это допустимо (retry разруливает);
content_hash делает повторные прогоны дешёвыми.

## Деградация (fail-open, как фазы 1/2)

| Ситуация | Поведение |
|---|---|
| Neo4j недоступен | `index_task` всё равно эмбедит в Postgres (`search_tasks` жив); граф-часть warn+skip. `get_task_context` → пусто + warning. `publish_review`-линковка warn+skip, ревью публикуется. |
| Postgres `tasks` проблема | граф-часть `index_task` всё равно проходит; `search_tasks` → пусто + warning. |
| Доска недоступна / задача не найдена | скилл (`review-pr`/`sync-tasks`) пропускает шаг (как фаза 2). |
| `task_board` не задан | контекст задач и индексирование выключены (тихо), фаза-1/2-поведение. |
| `index_task` получил мусор | возвращает `{warnings:[...]}`, не падает; прочие шаги скилла продолжаются. |

Ни один сценарий не валит прогон ревью/синка.

## Тестирование и эвал

### Unit (Python, фейки/моки, без сети)
- `TaskStore`: RRF-слияние BM25+ANN; дедуп по `content_hash` (skip embed при совпадении);
  `existing_hash`; `TaskRow`/`TaskHit`.
- `TaskGraph`: `upsert_task` (codes = key+aliases); `upsert_links` (+ стабы соседей);
  `link_pr` (`:PR`+`IMPLEMENTED_BY`+`TOUCHES`); `task_context` (резолв по alias, обход
  до PR и кода); деградация при `driver=None`.
- `TaskService.index_task`: форма отчёта; embed-skip; fail-soft по слоям.
- `publish_review`-линковка: при `task_key` зовёт `link_review` с `changed_node_ids` из
  сессии; dry-run и `task_key=None` — не зовёт; граф ↓ → warning, публикация идёт.
- Форматирование вывода `search_tasks`/`get_task_context` (включая «пусто»-ветки).

### Integration (маркер `integration`, живые Postgres+Neo4j)
- MCP stdio: `index_task` → `search_tasks` находит задачу → `get_task_context` отдаёт
  `TASK_LINK`-связи и (после ревью) PR.
- `tasks`-схема создаётся механизмом инициализации; `tasks_bm25`/`tasks_hnsw` работают.

### E2E / ручное (модели Sonnet/Haiku; Opus берегу)
- Живой Yougile MCP: `/sync-tasks` греет пару задач проекта «Пример проекта» (коды
  `PRI-*`/`ID-*`); `get_task_context`/`search_tasks` возвращают непустое.
- `review-pr` на PR со ссылкой на Yougile-задачу: задача индексируется, PR линкуется
  (`get_task_context` после ревью показывает PR + затронутый код), `requirements`-измерение
  обогащено связанными задачами.
- Деградация: тот же поток с отключённым Neo4j / board MCP → ревью и синк проходят, в
  сводке/логе корректные пометки.

Внешние сервисы (доска, Voyage) — только в E2E/ручном (конвенция проекта; unit мокает).

## Влияние на существующий код

**Меняется:**
- `reviewer/index/schema.sql` — таблица `tasks` + индексы (idempotent).
- `reviewer/app.py` — `Components` (+`task_store`/`task_graph`/`task_service`),
  `build_components` (сборка; шаринг DSN/Neo4j-драйвера).
- `reviewer/graph/store.py` — экспорт Neo4j-драйвера для шаринга с `TaskGraph`
  (или вынос создания драйвера в `build_components`).
- `reviewer/mcp/service.py` — делегаты `index_task`/`search_tasks`/`get_task_context`;
  `publish_review` (+опц. `task_key`, вызов `link_review`).
- `reviewer/entrypoints/mcp_server.py` — регистрация 3 новых тулов; `+task_key` в
  `publish_review`.
- `plugin/skills/review-pr/SKILL.md` — шаги persist/enrich/link.
- `plugin/skills/review-pr/references/requirements-prompt.md` — блок «related tasks».
- `plugin/skills/review-pr/references/task-context-yougile.md` — `aliases`, `links` из
  `subtasks[]`, `url` из `url_template`.
- `plugin/skills/review-pr/references/task-context-jira.md` — `aliases=[]`, issue-links.
- `README.md` / `.review.example.yml` — `url_template`, описание графа задач и `/sync-tasks`.

**Добавляется:**
- Пакет `reviewer/tasks/` (`store.py`, `graph.py`, `service.py`, `__init__.py`).
- Скилл `plugin/skills/sync-tasks/` (`SKILL.md`, `references/sync-tasks-yougile.md`).
- Тесты: `tests/tasks/` (store, graph, service), дополнения в `tests/mcp/` (новые тулы,
  линковка), integration.

**Без изменений:** ядро ревью фаз 1/2 (`assemble`, `dedup`, grounding, gate, история,
веб-админка); код-граф `:Symbol` и его построение; `chunks`/freshness base/overlay;
`task_keys.py` (извлечение ключей — уже ловит ссылки).

## Открытые вопросы для плана (не блокируют дизайн)

- Точное место создания Neo4j-драйвера при шаринге `GraphStore`↔`TaskGraph` (экспорт
  свойства vs вынос в `build_components`) — план выберет минимально инвазивный вариант.
- Формат структурированного вывода `get_task_context` (текст для агента) — финализируется
  в плане под `max_tool_result_chars`.
- Нужны ли `search_tasks`/`get_task_context` ревью-сабагентам напрямую (репо-глобальные,
  сессия не нужна) — план уточнит экспозицию в `make_tools`/скилле.

## Источники

- Фундамент: `2026-06-12-phase2-task-context-design.md`, `2026-06-12-phase2-task-context.md`,
  `plugin/skills/review-pr/references/task-context-yougile.md` (продакшен-плейбук, выверен E2E).
- E2E-факты Yougile: `subtasks[]` = UUID детей (единственная структурная связь);
  `links` в API нет; `columnId`→`get_column` для статуса; коды `idTaskProject` (`PRI-N`) /
  `idTaskCommon` (`ID-N`); web-ссылка `…/team/<teamId>/#PRI-4` (фрагмент = project code).
- Существующие структуры: `index/store.py` (`chunks`, RRF, HNSW/bm25), `graph/store.py`
  (`:Symbol`, CALLS/IMPLEMENTS), `mcp/service.py` (`_Session`, тулы), `app.py`
  (`Components`/`build_components`).
