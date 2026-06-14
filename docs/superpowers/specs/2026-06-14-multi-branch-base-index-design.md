# Дизайн: мультибранч base-индекс и маршрутизация ревью по целевой ветке

- **Дата:** 2026-06-14
- **Статус:** одобрено к реализации
- **Затрагивает:** `reviewer/config/settings.py`, `.env.example`, `reviewer/index/store.py`,
  `reviewer/index/freshness.py`, `reviewer/index/schema.sql`, `reviewer/graph/store.py`,
  `reviewer/graph/backend.py`, `reviewer/retrieval/retriever.py`, `reviewer/services/review_service.py`,
  `reviewer/mcp/service.py`, `reviewer/entrypoints/cli.py`, `reviewer/services/repo_id.py` (новый хелпер
  ветки рядом), скиллы `plugin/` (solve-task), тесты, `README.md`/`CLAUDE.md`.

## 1. Цель

Сейчас деплой мультирепо, но **однобранчевый**: на репо существует ровно один base-индекс под
жёстким `ref="base"`. PR в `master`/`release/*` конкурируют за этот единственный индекс — второй PR
перезаписывает SHA первого, а код одной ветки ревьюится против чанков другой.

Нужна поддержка **нескольких отслеживаемых веток** (2–3), настраиваемых через `.env`. Кейс: в `main`
льётся редко (релиз), а готовые версии сливают в `master`/`develop` — их PR тоже надо ревьюить, против
**их собственной** базы.

### Сквозной принцип

`ref` уже работает как дискриминатор `вид:значение` (`"base"`, `"pr:N"`). Обобщаем `"base"` →
`"base:<branch>"`. `branch` — **ортогональное измерение** рядом с `repo`: навязывается ВНУТРИ хранилищ,
в промпты агента не утекает (агент по-прежнему оперирует `path#fqn`). Имя целевой ветки PR
(`prq.base_ref`) уже известно в `prepare` и уже используется для чтения `.review.yml` — не хватает
только изоляции индекса.

## 2. Зафиксированные решения

| # | Решение | Выбор | Почему |
|---|---|---|---|
| 1 | Ключ ветки в хранилище | `ref = "base:<branch>"` (не новая колонка) | Git запрещает `:` в именах веток → парсинг однозначен; схема Postgres (PK/UNIQUE по `ref`) не меняется; локализует правку |
| 2 | PR в неотслеживаемую ветку | **Пропускать ревью** (skip, запись причины в историю) | Чёткая модель: ревьюим только интеграционные ветки из allowlist; не тратим Voyage |
| 3 | Граф кода (Neo4j) | **Отдельный на ветку**: `:Symbol{repo, branch, id}` | Корректность при расхождении веток; рёбра считает tree-sitter дёшево |
| 4 | Список веток | Глобальный allowlist в `.env` (`REVIEW_BRANCHES`), первая = первичная | Соответствует просьбе (настройка через `.env`); для мультирепо это allowlist |
| 5 | Ветка для solve-task / CLI search | Текущая git-ветка клона, если в allowlist; иначе первичная; override параметром | «Ветвимся относительно текущей ветки»; параметр задаёт явно |
| 6 | Стоимость Voyage | Переиспользование эмбеддингов между ветками по `content_hash` | Ветки перекрываются ~95%; стоимость N веток ≈ 1 + дельты |
| 7 | Существующие данные | Идемпотентная миграция `ref="base"` → `base:<первичная>` без переэмбеддинга | Voyage дорогой; сохраняем рабочий индекс |
| 8 | Индекс ветки не построен | Fail-soft: ревью на overlay + предупреждение «запустите `reviewer index`» | Согласуется с текущим fail-soft стилем self-heal |

## 3. Текущее состояние (база отсчёта)

- **Postgres `chunks`:** `UNIQUE (repo, ref, path, symbol_fqn)`, `ref ∈ {"base","pr:N"}`. Индексы
  `chunks_repo_ref_path`, BM25/HNSW.
- **Postgres `index_meta`:** `PRIMARY KEY (repo, ref)` — на практике пишется только `(repo,"base")`.
- **`freshness.update_base(store, embedder, repo, target_ref, ...)`** принимает `target_ref` (имя
  ветки PR), но **игнорирует** его — всё пишет в литерал `"base"` (строки ~59,61,69,71,73).
- **`store.hybrid_search` / `fetch_nodes`:** `WHERE ref='base'` (литерал), overlay по `ref=pr:N`.
- **`review_service.prepare`:** `get_index_meta(repo,"base")`, self-heal сравнивает с `prq.base_sha`,
  при расхождении `update_base(...)` + `set_index_meta(repo,"base",base_sha)`. `.review.yml` читается
  из `prq.base_ref` (**уже корректно пер-бранч**).
- **Neo4j:** `:Symbol{repo, id}` — ветки нет; составная уникальность `(repo, id)`.
- **MCP-граница несёт repo:** `prepare_review(repo, pr)`, сессии ключуются `(repo, pr)`. PR несёт
  `base_ref`/`base_sha` (`vcs/base.py::PullRequest`, `vcs/github.py::get_pull_request`).
- **Settings:** есть паттерн CSV→list (`_csv`, `review_categories_list`). Маппинг env по имени поля
  UPPER_CASE. `default_repo` — дефолт-репо для session-less операций.

## 4. Конфигурация

`reviewer/config/settings.py`:

```python
review_branches: str = "main"   # CSV; первая = первичная

def review_branches_list(self) -> list[str]:
    return self._csv(self.review_branches) or ["main"]

def primary_branch(self) -> str:
    return self.review_branches_list()[0]
```

- Пустое/незаданное → `["main"]` (бэк-совместимость).
- `.env.example`: новая секция
  ```env
  # Отслеживаемые ветки для ревью (CSV). Первая — первичная (дефолт для CLI search / solve-task).
  # PR в ветку вне этого списка ревью пропускает.
  REVIEW_BRANCHES=main,master
  ```

## 5. Слой хранения

### 5.1 Хелпер ключа

Единый хелпер (рядом с `repo_id.py`, напр. `reviewer/index/refs.py`):

```python
def base_ref(branch: str) -> str:        # "main" -> "base:main"
    return f"base:{branch}"
```

Заменяет все 7 литералов `"base"`. Overlay `pr:N` не трогаем (номер PR уникален в репо независимо от
ветки).

### 5.2 Postgres

Схема (`schema.sql`) **не меняется** по PK/UNIQUE — `(repo, ref, ...)` уже несёт ветку через `ref`.
Параметризуются вызовы:

- `freshness.update_base(...)` — наконец **использует** `target_ref`: пишет в `base_ref(target_ref)`
  во всех внутренних вызовах (`delete_paths`, `existing_hashes`, `_rows_for_file`,
  `delete_missing_symbols`).
- `store.hybrid_search` / `fetch_nodes` — WHERE принимает параметр `base_ref` вместо литерала:
  `((ref=%(base)s AND NOT path=ANY(%(changed)s)) OR ref=%(overlay)s)`.
- CLI `index`: `delete_paths_except(repo, base_ref(branch), files)`,
  `set_index_meta(repo, base_ref(branch), sha)`.

### 5.3 Переиспользование эмбеддингов между ветками (раздел 2 №6)

При ingest ветки B для чанка с `content_hash` H: если в любом `base:*` того же `repo` уже есть строка
с этим хешем — **копируем готовый вектор** вместо вызова Voyage (эмбеддинг детерминирован по тексту
чанка при фиксированной модели; в рамках деплоя модель одна).

- Новый метод `store.find_embeddings_by_hashes(repo, hashes) -> dict[hash, vector]`.
- В `freshness` ingest: сначала добираем кэш по хешам из других веток, эмбеддим Voyage только остаток.

### 5.4 Neo4j (граф на ветку)

- `:Symbol{repo, branch, id}`; уникальность `(repo, branch, id)`.
- `GraphStore.upsert_nodes/upsert_edges/clear` и запрос соседей (graph-expansion) получают `branch`.
- `reviewer index` полностью перестраивает граф для `(repo, branch)` (clear+upsert этой пары).
- Self-heal графа на `prepare` — `(repo, branch)`-scoped (tree-sitter, fail-soft, как сейчас).
- `retrieval.Retriever` graph-expansion фильтрует по `(repo, branch)`.

## 6. Маршрутизация ревью

`ReviewService.prepare` / `MCPReviewService.prepare_review`:

1. **`prq.base_ref ∈ review_branches_list()`** → `branch = prq.base_ref`. Self-heal индекса/графа для
   `base_ref(branch)` против `prq.base_sha`. Ретрив/overlay по этой ветке.
2. **`prq.base_ref ∉` списка** → **skip**: не строим overlay, не публикуем; возвращаем
   `{status: "skipped", reason: "branch '<x>' not tracked"}`; пишем в `review_runs` (fail-soft).
3. **Индекс ветки не построен** (`get_index_meta(repo, base_ref(branch))` = None, но ветка в allowlist)
   → fail-soft: ревью на overlay изменённых файлов PR; в сводку/историю — предупреждение
   «base-индекс ветки `<x>` не построен, контекст ограничен; запустите `reviewer index --ref <x>`».

Skip должен сработать **до** дорогих шагов (ingest overlay, эмбеддинги) — проверка ветки в начале
`prepare`.

## 7. solve-task / CLI search (ветка-агностичные операции)

Единый резолвер ветки:

```python
def resolve_branch(requested, current_git_branch, settings) -> str:
    allow = settings.review_branches_list()
    if requested:
        if requested not in allow:
            raise ValueError(f"ветка {requested!r} не в REVIEW_BRANCHES ({allow})")
        return requested
    if current_git_branch in allow:
        return current_git_branch
    return settings.primary_branch()
```

- **Скилл `solve-task`** (`plugin/`, исполняется в Claude Code в проекте пользователя): определяет
  текущую git-ветку клиента (`git branch --show-current`); если в allowlist — ветвимся относительно
  неё; параметр позволяет указать явно, «от какой ветвимся». Резолвленная ветка передаётся в
  `search_codebase`/любой код-ретрив как `branch`.
- **MCP `search_codebase(repo, query, branch=None)`**: `branch` явно задан → используем; `None` →
  первичная (сервер не знает cwd клиента — текущую ветку резолвит скилл и передаёт).
- **CLI `search`**: `--branch` override; дефолт — текущая git-ветка клона если в allowlist, иначе
  первичная.
- **Граф задач `:Task` — остаётся глобальным, branch-agnostic** (задача описывает *что*, не *против
  какой ветки*). Ветвится только код-ретрив.

## 8. Индексация (CLI)

- `reviewer index <path> --ref <branch>` — `--ref` задаёт и что читать, и ключ ветки `base:<branch>`.
  Дефолт `--ref` меняется `HEAD` → `settings.primary_branch()`.
- Доп. `--branch <name>` (override ключа): читать из `--ref`, хранить под `--branch` (edge: detached
  HEAD/SHA). Дефолт `--branch` = `--ref`.
- Каждая ветка индексируется отдельным вызовом; `reviewer check` выводит, какие ветки построены.

## 9. Миграция существующих данных (раздел 2 №7)

Идемпотентная одноразовая миграция (команда `reviewer migrate-branches` или авто-проверка при старте):

- Postgres: `UPDATE chunks SET ref = 'base:<primary>' WHERE ref = 'base'`; то же для `index_meta`.
- Neo4j: `MATCH (s:Symbol) WHERE s.branch IS NULL SET s.branch = '<primary>'`; затем привести
  constraint к `(repo, branch, id)`.
- `<primary>` = `settings.primary_branch()`. Без переэмбеддинга — векторы сохраняются.

## 10. Тестирование (TDD)

**unit (фейки, без сети):**
- `review_branches_list` парсинг (CSV, пусто→`["main"]`); `primary_branch`; `base_ref()`.
- `resolve_branch`: requested-в-списке / requested-вне / текущая-в-списке / фоллбэк-первичная.
- routing: tracked→review, untracked→skip(payload+история), not-built→warn+overlay.
- `hybrid_search`/`fetch_nodes` изоляция по ветке (расширить `tests/index/test_store_hybrid.py`:
  две ветки одного репо не видят чанки друг друга).
- `update_base` пишет в `base:<target_ref>`, не в `"base"` (обновить `tests/index/test_freshness.py`).
- cross-branch reuse: фейк-embedder не вызывается для совпавших `content_hash` второй ветки.
- граф branch-scope: `:Symbol` разных веток изолированы.

**integration (поднятые стораджи):**
- индекс двух веток одного репо → изоляция; миграция legacy `ref="base"` → `base:<primary>`.

## 11. Затронутые файлы

`config/settings.py`, `.env.example`, `index/refs.py` (новый), `index/store.py`, `index/freshness.py`,
`index/schema.sql` (комменты/индексы, не PK), `graph/store.py`, `graph/backend.py`/`builder.py`
(проброс branch), `retrieval/retriever.py`, `services/review_service.py`, `mcp/service.py`,
`entrypoints/cli.py`, `plugin/` (skill solve-task), тесты `tests/index/*`, `tests/config/*`,
`tests/services/*`, `README.md`, `CLAUDE.md`.

## 12. Вне области (YAGNI)

- Пер-репо переопределение списка веток (через `.review.yml`) — глобального allowlist достаточно.
- Авто-обнаружение/авто-индекс всех веток в один проход — ветки индексируются явными вызовами.
- Ветвление графа задач `:Task` — задачи кросс-репо и кросс-бранч по природе.
- Ленивое построение индекса на лету при PR в неотслеживаемую ветку — выбран skip.
