# PRI-159 — GraphRAG-сводки подсистем (community summaries) — дизайн

**Задача:** PRI-159 (ID-159), оценка M–L. Слой: Движок (reviewer CLI/MCP) → ask/walkthrough.
**Ветка работы:** `feat/graphrag-summaries-walkthrough`.
**Связанная задача:** PRI-119 (PR-walkthrough) — второй потребитель summary; проектируется отдельным спеком
`2026-06-23-pri-119-pr-walkthrough-design.md`. API summary в этом спеке нейтрален к потребителю.

## Проблема

Высокоуровневые вопросы («как устроена подсистема X») в `ask`/онбординге и PR-walkthrough требуют
обзора подсистемы, который дорого собирать на лету из чанков (много шагов разведки, шумный ретрив).
GraphRAG-подход: **предрасчёт кратких summary по кластерам графа кода** оффлайн; потребители берут
summary как дешёвый высокоуровневый приор **до** детального ретрива.

**Критерий приёмки (из задачи):** на архитектурный вопрос `ask` отвечает с опорой на summary
подсистемы — меньше шагов разведки, выше качество обзора.

## Решения (зафиксированы на brainstorming)

1. **Объём:** полная фича — кластеризация + summary + хранилище + интеграция в `ask`; API summary
   спроектирован так, чтобы PRI-119 переиспользовал его без переделки.
2. **Генерация summary — скилл-оркестрация** (а не server-side LLM): ядро reviewer не имеет
   general-purpose LLM (только Voyage embed/rerank). Python детерминированно кластеризует и отдаёт
   материал; LLM в Claude Code пишет текст; MCP-тул персистит. Без новых зависимостей/ключей на
   сервере, в духе `review-pr`/`sync`.
3. **Кластеризация — по модулям/пути** (кластер = пакет/директория через префикс `node_id="path#fqn"`),
   глубина настраивается. Дёшево, стабильно, интерпретируемо; совпадает с таблицей модулей в README.
4. **Хранилище — новая таблица** `subsystem_summaries` (Postgres), per `(repo, branch)`, с
   `source_hash` для инкрементальной свежести.
5. **Ретрив у потребителя — fetch-all + by-key** (без вектор-поиска в MVP): тул отдаёт список всех
   кластеров (key + title + summary); LLM выбирает нужный. Колонка `embedding` зарезервирована под
   будущий вектор-поиск (без миграции).

## Архитектура и поток данных

Две фазы, обе скилл-оркестрируемые.

### BUILD (новый скилл `/reviewer_summarize-subsystems`)

```
skill → list_subsystem_clusters(repo, branch)        [MCP → Python: кластеры + материал + freshness]
      → для каждого STALE-кластера:
          LLM читает представительные файлы (Read) и пишет title + summary (RU, grounded)
      → index_subsystem_summary(repo, branch, key, title, summary, source_hash)  [MCP → Python: upsert]
```
Свежие кластеры (`stale=false`, `source_hash` совпал) пропускаются → инкрементально.
Стоимость по Voyage = **0** (эмбеддингов summary в MVP нет); тратятся только LLM-токены скилла.

### CONSUME (`ask`; далее PRI-119)

```
ask → get_subsystem_summaries(repo, branch)           [MCP → Python: дешёвый приор]
    → LLM берёт релевантную подсистему как ориентир → меньше шагов разведки
    → детальный search_codebase/expand как сейчас (citations — из реального кода)
```

## Компоненты

| Компонент | Тип | Роль |
|---|---|---|
| `reviewer/graph/summaries.py` | новый | Чистая логика кластеризации и `source_hash`. `build_clusters(chunk_store, graph, repo, branch, *, depth, min_size) -> list[Cluster]` |
| `reviewer/index/summary_store.py` | новый | `SummaryStore` — CRUD таблицы `subsystem_summaries` (по образцу `TaskStore`, свой пул) |
| `reviewer/index/schema.sql` | правка | DDL `subsystem_summaries` (создаётся на `reviewer index` через `ChunkStore.init_schema`) |
| `reviewer/index/store.py` | правка | `ChunkStore.list_base_members(repo, branch)` — состав base-индекса для кластеризации |
| `reviewer/mcp/service.py` | правка | методы `list_subsystem_clusters`, `index_subsystem_summary`, `get_subsystem_summaries` |
| `reviewer/entrypoints/mcp_server.py` | правка | тонкие `@mcp.tool()`-обёртки над методами выше |
| `reviewer/app.py` | правка | `Components.summary_store: SummaryStore`; сборка в `build_components` (как `task_store`) |
| `plugin/skills/summarize-subsystems/SKILL.md` | новый | BUILD-скилл (RU-вывод; `_common`-include'ы) |
| `plugin/skills/ask/SKILL.md` | правка | шаг-приор `get_subsystem_summaries` (fail-open) |

**Почему членство из Postgres-чанков, а не из Neo4j:** путь есть и в `chunks`, и в `:Symbol`; кластеризация
по пути требует только путей. Берём из `chunks` (`ref="base:<branch>"`) → надёжно (не падает при
недоступном Neo4j). Граф нужен лишь для ранжирования центральных символов (`GraphStore.in_degree`) и
деградирует мягко.

## Модель данных

`reviewer/index/schema.sql` (добавить; зеркалит `tasks`/`index_meta`):

```sql
CREATE TABLE IF NOT EXISTS subsystem_summaries (
    repo            text    NOT NULL DEFAULT '',
    branch          text    NOT NULL,
    cluster_key     text    NOT NULL,            -- напр. "reviewer/index"
    title           text    NOT NULL,            -- одна строка «что это»
    summary         text    NOT NULL,            -- сжатый абзац (RU)
    member_node_ids text[]  NOT NULL DEFAULT '{}',
    source_hash     text    NOT NULL,            -- ключ свежести
    embedding       vector(1024),                -- nullable; зарезервировано под вектор-поиск
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, branch, cluster_key)
);
```

## Контракты

### Кластеризация (`reviewer/graph/summaries.py`)

- `@dataclass Cluster`: `key: str`, `member_node_ids: list[str]`, `files: list[str]`,
  `top_symbols: list[dict]` (`{node_id, file, line}`), `num_members: int`, `source_hash: str`.
- `cluster_key(path, depth) -> str`: директория пути, обрезанная до первых `depth` сегментов.
  `"reviewer/index/store.py", depth=2 → "reviewer/index"`. Файл в корне (`"setup.py"`) или директория
  короче `depth` → группа = вся директория (`"."` для корня нормализуем в `"<root>"`). Детерминирована.
- `source_hash` = `sha256` от join'а `sorted("{node_id}:{content_hash}")` по членам кластера →
  меняется только при изменении состава/содержимого подсистемы. Использует существующий
  `chunks.content_hash` (тот же дедуп-инвариант).
- `top_symbols`: топ-N членов по `GraphStore.in_degree` (центральность = сколько мест зависит;
  N — небольшая константа, default 10). Fail-soft: граф `None`/ошибка → упорядочить по
  `(path, start_line)`, `in_degree` опустить.
- `min_size`: кластеры с `num_members < min_size` отбрасываются (default 1 = не отбрасывать;
  настраивается). `depth` default 2.

### `ChunkStore.list_base_members(repo, branch) -> list[tuple[path, symbol_fqn, content_hash, start_line]]`

`SELECT path, symbol_fqn, content_hash, start_line FROM chunks WHERE repo=%s AND ref=%s` для
`ref=base:<branch>` (через `reviewer.index.refs.base_ref`). `node_id = f"{path}#{symbol_fqn}"`.

### `SummaryStore` (`reviewer/index/summary_store.py`)

- `upsert_summary(repo, branch, cluster_key, title, summary, member_node_ids, source_hash)` —
  UPSERT по `(repo, branch, cluster_key)`, обновляет `updated_at`.
- `get_source_hashes(repo, branch) -> dict[cluster_key, source_hash]` — для вычисления `stale`.
- `get_summaries(repo, branch) -> list[dict]` — все: `{cluster_key, title, summary, updated_at}`.
- `get_summary(repo, branch, cluster_key) -> dict | None` — один полный.
- Отсутствие таблицы (старый индекс без `init_schema`) трактуется как пусто (fail-soft, как
  `get_index_meta`).

### MCP-тулы (`service.py` методы + `mcp_server.py` обёртки)

- `list_subsystem_clusters(repo, branch=None, depth=None, min_size=None) -> dict` →
  `{"clusters": [{cluster_key, num_members, files, top_symbols, source_hash, stale}], "branch": ...}`.
  `stale` = `source_hash != stored` (или нет stored). Пустой base-индекс → `{"clusters": [], "note": "..."}`.
- `index_subsystem_summary(repo, branch, cluster_key, title, summary, source_hash) -> dict` →
  `{cluster_key, stored: true}`. Idempotent.
- `get_subsystem_summaries(repo, branch=None, cluster_key=None) -> dict` →
  `cluster_key=None`: `{"summaries": [{cluster_key, title, summary}]}` (приор для всех);
  `cluster_key`: один полный объект или `null`. Пусто/нет таблицы → `{"summaries": []}` (потребитель
  fail-open). `branch=None` → первичная ветка (как у session-less тулов).

### Скилл `plugin/skills/summarize-subsystems/SKILL.md`

- Тело — на английском (токены), вывод пользователю и **тексты summary — на русском** (язык проекта;
  их потребляет `ask`, отвечающий по-русски).
- Резолв repo/branch (include `_common/branch-selection.md`).
- (опц.) preflight-свежесть `reviewer status --json` → если `drift>0` — баннер (как в `ask`),
  не блокировать.
- `list_subsystem_clusters` → по `stale`-кластерам: `Read` представительных файлов (из `files`/
  `top_symbols`) для grounding → написать `title` (1 строка) + `summary` (сжатый абзац, что делает
  подсистема, ключевые символы/инварианты) → `index_subsystem_summary`. Свежие — пропустить.
- Anti-hallucination: include `_common/anti-hallucination.md` — summary опирается на реальный код.
- Fail-open: нет графа → членство всё равно есть; нет base-индекса → сообщить «сначала
  `/reviewer_sync-codebase`».

### Интеграция в `ask` (`plugin/skills/ask/SKILL.md`)

- Новый шаг-приор после резолва repo/branch: вызвать `get_subsystem_summaries(repo, branch)`.
  Непусто → для архитектурного/«как устроена X» вопроса выбрать релевантную подсистему и
  использовать как ориентир до `search_codebase` (меньше шагов expand).
- **Grounding-контракт без изменений:** summary — приор, но любые `path:line` в ответе по-прежнему
  только из реального кода (`search_codebase`/`Read`). Summary сам сгенерирован из кода, но не
  является источником цитат.
- Fail-open: пусто/недоступно → `ask` работает как сегодня.

## Обработка ошибок / краевые случаи

- Нет base-индекса/графа → `list_subsystem_clusters` пуст + подсказка `reviewer index`.
- Neo4j down → членство из чанков; `top_symbols` деградирует до порядка по файлам/строкам.
- `ask` fail-open при пустых summary.
- Всё per `(repo, branch)` — мульти-бранч/мульти-репо изоляция.
- Повторный BUILD идемпотентен: совпавший `source_hash` → пропуск кластера.

## Тестирование

- **Unit:** `cluster_key` (пути/глубины/корень/край); детерминизм `source_hash` + детект изменения
  состава/контента; `Cluster.top_symbols` fail-soft при `graph=None`; `SummaryStore` CRUD
  upsert/get/get_source_hashes (на тестовом Postgres или фейке); сборка `list_subsystem_clusters`
  (фейк-стор + фейк-граф) включая флаг `stale`.
- **Guard-тест** сборки промпта нового скилла (`tests/skills/`, как у существующих скиллов —
  раскрытие `_common`-include'ов).
- **Integration** (маркер `integration`): round-trip таблицы `subsystem_summaries` на реальном Postgres.

## Вне объёма (YAGNI / на потом)

- Вектор-поиск по summary (колонка `embedding` зарезервирована, эмбеддинг не считается).
- Кластеризация по связности (Louvain/Leiden) — текущий выбор по модулям достаточен.
- Отдельная CLI-подкоманда генерации (триггер — скилл). Опционально позже: показ свежести summary в
  `reviewer status`.
- server-side вызов Anthropic SDK.
