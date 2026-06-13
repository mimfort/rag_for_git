# Дизайн: мульти-репо хранилище + авто-реиндекс графа

- **Дата:** 2026-06-13
- **Статус:** одобрено к реализации
- **Затрагивает:** `reviewer/index/`, `reviewer/graph/`, `reviewer/retrieval/`, `reviewer/tools/`,
  `reviewer/mcp/`, `reviewer/services/`, `reviewer/entrypoints/cli.py`, `reviewer/config/settings.py`,
  `reviewer/tasks/graph.py`, схемы Postgres/Neo4j, оба README, `.env.example`.

## 1. Цель

Две связанные фичи на слое хранения:

- **F1 — Мульти-репо.** Один деплой (одна БД Postgres + один Neo4j) обслуживает **N изолированных
  репозиториев**, выбираемых по репозиторию PR. Кейс — микросервисы с несколькими репами.
- **F2 — Авто-реиндекс графа.** Сейчас на `prepare_review` самолечатся только векторные чанки, а
  граф кода (Neo4j) дрейфит до ручного `reviewer index`. Добавляем **инкрементальный repo-aware
  патч графа** в тот же блок дрейфа.

Порядок сборки: **F1 — фундамент** (меняет ключи и схемы), **F2 — поверх** (repo-aware с самого
начала).

### Сквозной принцип

`node_id = "path#fqn"` остаётся **agent-facing инвариантом**: тулы, промпты, grounding, fingerprint
не меняются. `repo` — **ортогональное измерение**, навязывается ВНУТРИ хранилищ. LLM по-прежнему
оперирует `path#fqn`; `repo` берётся из сессии и нигде в промпт не утекает.

## 2. Зафиксированные решения

| # | Решение | Выбор | Почему |
|---|---|---|---|
| 1 | Авто-реиндекс графа | Инкрементальный tree-sitter на prepare | Симметрия с self-heal векторов; без новой инфры/клона |
| 2 | Мульти-репо изоляция | Общая БД + дискриминатор `repo` | Работает на Neo4j Community; сохраняет `node_id` инвариант |
| 3 | Repo identity при индексации | Явный `--repo owner/name` + derive из git remote как fallback | Однозначно; derive страхует от опечаток |
| 4 | Кросс-репо ретрив | Нет — каждое ревью строго в рамках своего репо | YAGNI; SCIP/tree-sitter строят граф только в пределах одного репо |

## 3. Текущее состояние (база отсчёта)

- **Postgres `chunks`:** ключ `UNIQUE (ref, path, symbol_fqn)`, `ref ∈ {"base","pr:N"}`. Колонки
  `repo` нет.
- **Postgres `index_meta`:** `PRIMARY KEY (ref)` — SHA base-индекса, без repo.
- **Neo4j:** `:Symbol {id="path#fqn"}` с `CONSTRAINT sym_id` (id уникален); рёбра
  `CALLS`/`IMPLEMENTS`/`TESTED_BY`. `:Task {key, codes}`, `:PR {id}`. Без repo.
- **Self-heal векторов** (`review_service.prepare`, текущий код): сравнивает
  `index_meta["base"].sha` с `prq.base_sha`; при расхождении зовёт GitHub compare API и
  `update_base` по изменившимся файлам. **Граф не трогается.**
- **MCP-граница уже несёт repo:** `prepare_review(repo, pr)`, сессии ключуются `(repo, pr)`.
- **`review_runs` уже хранит `repo`** (см. `_record_history`) — наблюдаемость почти не трогаем.

## 4. F1 — Мульти-репо

### 4.1 Repo identity и нормализация

- Канонический тег — `owner/name`, **нормализуется к нижнему регистру** на границе (`repo.strip()
  .lower()`), чтобы `Owner/Repo` и `owner/repo` не плодили два namespace. Нормализация — в одном
  helper'е (`reviewer/services/repo_id.py::normalize_repo`), переиспользуется CLI и MCP.
- `reviewer index <path> --repo owner/name` задаёт тег явно.
- Без `--repo` — derive из `git remote get-url origin`: парсим обе формы
  (`git@github.com:owner/name.git`, `https://github.com/owner/name(.git)?`) regex'ом
  `github\.com[:/]([^/]+)/([^/.]+)`. Не удалось (нет remote / не github / неоднозначно) — **ошибка с
  внятным сообщением** («укажите --repo owner/name»), не молчаливый дефолт.

### 4.2 Postgres

**`chunks`:** добавить `repo TEXT NOT NULL`; ключ `UNIQUE (ref, path, symbol_fqn)` →
`UNIQUE (repo, ref, path, symbol_fqn)`. Индекс `chunks_ref_path` → `(repo, ref, path)`.
BM25-индекс добавить `repo` в список stored-полей.

**`index_meta`:** `PRIMARY KEY (ref)` → `PRIMARY KEY (repo, ref)`; добавить `repo TEXT NOT NULL`.

**`ChunkStore` — все методы получают `repo` и фильтруют по нему:**
`upsert` (в INSERT + ON CONFLICT target `(repo, ref, path, symbol_fqn)`), `existing_hashes(repo, ref)`,
`delete_ref(repo, ref)`, `delete_paths(repo, ref, paths)`, `delete_missing_symbols(repo, ref, path, keep)`,
`delete_paths_except(repo, ref, keep)`, `hybrid_search(repo, …)` (добавить `AND repo=%(repo)s` в оба
CTE-WHERE), `fetch_nodes(repo, …)` (добавить `AND c.repo=%(repo)s`), `get_index_meta(repo, ref)`,
`set_index_meta(repo, ref, sha)`. `clear(repo)` — DELETE по repo (не TRUNCATE); для тестов оставить
возможность `clear(repo=None)` → TRUNCATE.

`ChunkRow` получает поле `repo`.

### 4.3 Neo4j

**`:Symbol`:** добавить свойство `repo`. `MERGE` по `{repo, id}` (один `id` в разных репо — разные
узлы). Заменить `CONSTRAINT sym_id` (id уникален) на composite uniqueness
`CONSTRAINT sym_repo_id … REQUIRE (s.repo, s.id) IS UNIQUE` (доступно в Neo4j 5 Community как
property-uniqueness). Миграция: `DROP CONSTRAINT sym_id IF EXISTS` перед созданием нового.

**`GraphStore` — repo прокидывается во все методы:** `upsert_nodes(repo, ids)` (MERGE `{repo,id}`),
`upsert_edges(repo, edges)` (MATCH узлов по `{repo,id}`), `expand(repo, ids, hops)`,
`callers(repo, ids)`, `find_symbol(repo, name)`, `clear(repo)` — все добавляют `{repo:$repo}` в
паттерны / `WHERE s.repo=$repo`.

**Новые методы для F2** (см. §5): `symbols_for_paths(repo, paths)`, `delete_symbols(repo, ids)`,
`delete_outgoing_calls(repo, ids)`.

### 4.4 Граф задач — намеренно НЕ изолируем по репо

`:Task` остаётся **глобальным**: одна задача может закрывать PR-ы в нескольких микросервисах (это
прямо ваш кейс). `:PR` уже носит repo (`PRRef.repo`). Меняется только линковка кода: рёбра `TOUCHES`
ведут к `:Symbol{repo: pr_ref.repo, id: node_id}` — `TaskGraph.link_review` (и связанные обходы
`get_task_context`) матчат `:Symbol` с учётом repo PR-а. Так задача-агрегатор видит код во всех
своих репах, но каждый символ резолвится в правильном namespace.

### 4.5 Retriever / ToolContext

- `ToolContext` получает поле `repo: str`.
- `Retriever.retrieve(repo, …)` и `Retriever.search_base(repo, …)` пробрасывают `repo` во все
  `store.hybrid_search` / `store.fetch_nodes` / `graph.expand`.
- `make_tools` использует `ctx.repo` во всех тулах (`search_code`, `get_related_symbols`,
  `get_definition`, `find_callers`). `ctx_sig` (ключ кэша) дополняется `repo` — кэш не должен
  смешивать репо.

### 4.6 MCP-сервис и тулы

- `MCPReviewService.prepare_review(repo, pr)` — нормализует `repo`, кладёт в сессию и в `ToolContext`
  (`_tool_context` берёт `repo` из `prepared`). `PreparedReview` получает поле `repo`.
- Все session-тулы (`search_code`, `get_related_symbols`, …) берут `repo` из сессии — сигнатуры MCP
  не меняются (repo и так в `(repo, pr)`-ключе).
- `_cleanup` / self-heal overlay: `delete_ref(repo, f"pr:{pr}")` — repo-scoped, чтобы overlay одного
  репо не затирал overlay другого с тем же номером PR.
- **`search_codebase` (session-less, для `/solve-task`)** получает параметр `repo: str`. Сигнатура
  MCP-тула: `search_codebase(repo, query, top_k)`. Если плагин/скилл не передаёт repo — fallback на
  `settings.default_repo` (см. §4.8); пусто и там → внятная ошибка в тексте результата.

### 4.7 `review_service.prepare`

`repo = normalize_repo(f"{owner}/{name}")`. Пробросить во все обращения к store
(`delete_ref`, `get/set_index_meta`, `build_overlay`, `update_base`) и в блок дрейфа графа (F2).
`PreparedReview.repo = repo`. `build_overlay` / `update_base` (`freshness.py`) получают `repo` и
кладут его в `ChunkRow.repo`.

### 4.8 Settings

- Новая опц. настройка `DEFAULT_REPO` (`default_repo: str = ""`): мост для одно-репных деплоев —
  fallback для `search_codebase` и дефолт для `reviewer index --repo`. Пусто = мульти-репо-режим,
  repo обязателен явно/через derive.

### 4.9 CLI

`reviewer index <path> --ref <ref> [--repo owner/name]`: резолв repo (флаг → derive → `default_repo`
→ ошибка). Все вызовы `store.*` и `graph.*` в команде `index` — с repo. `graph.clear()` →
`graph.clear(repo)` (полный rebuild только этого репо, не всего инстанса). `reviewer search <query>`
получает `--repo` (диагностика по конкретному репо; дефолт — `default_repo`).

### 4.10 Миграция / back-compat

`chunks` и граф — **производные артефакты** (пересобираются из исходников). Стратегия:

- **Forward-only схема:** `ALTER TABLE chunks ADD COLUMN IF NOT EXISTS repo TEXT`; пересоздание
  unique-индекса и graph-constraint идемпотентно (`IF EXISTS`/`IF NOT EXISTS`).
- **Существующий индекс пересобирается** одним `reviewer index --repo owner/name` (дёшево — это
  derived store). На этапе перехода: если `DEFAULT_REPO` задан, `init_schema` бэкфиллит
  `chunks.repo` / `index_meta.repo` / `:Symbol.repo` значением `DEFAULT_REPO` для строк без repo —
  одно-репный деплой продолжает работать без ручного реиндекса.
- В README/`.env.example` зафиксировать: мульти-репо требует одноразового реиндекса каждого репо.

## 5. F2 — Авто-реиндекс графа (инкрементальный, repo-aware)

### 5.1 Триггер

Тот же блок в `review_service.prepare`, что лечит вектора (`vcs_provider is None and indexed and
indexed != prq.base_sha`). Те же `diff_files` и те же уже-загруженные исходники
(`vcs.get_file_at_ref(p, base_sha)`). Гоняется только при дрейфе SHA; проверка (сравнение SHA)
бесплатна.

### 5.2 Алгоритм патча (батч по всем изменённым файлам разом)

Поведение `build_graph_from_files` на **частичном** наборе (только изменённые файлы) изучено:
исходящие `CALLS` резолвятся в пределах переданного набора; цели в неизменённых файлах не резолвятся
(их нет в `files`) — **висячих рёбер не возникает** (fallback `name_to_nodes` содержит только
символы переданных файлов).

Наивный `DETACH DELETE` всех символов изменённого пути СТЁР БЫ валидные **входящие** `CALLS` от
неизменённых вызывающих — это сломало бы `find_callers` (ключевой impact-анализ). Поэтому патч
**сохраняет входящие рёбра**:

1. `nodes, edges = build_graph_from_files(changed_sources)` — только изменённые `.py` (источники
   head/base уже на руках).
2. `old = graph.symbols_for_paths(repo, changed_paths)` — текущие узлы этих путей.
3. `stale = old − nodes` → `graph.delete_symbols(repo, stale)` (`DETACH DELETE` исчезнувших/
   переименованных символов вместе с их рёбрами — они больше не существуют).
4. `graph.delete_outgoing_calls(repo, nodes)` — снести только ИСХОДЯЩИЕ `CALLS` у символов
   изменённой поверхности (входящие не трогаем).
5. `graph.upsert_nodes(repo, nodes)` + `graph.upsert_edges(repo, edges)` — свежие узлы и исходящие
   рёбра.
6. Удалённые из PR файлы (`status == "removed"`): `graph.delete_symbols(repo,
   symbols_for_paths(repo, [path]))`.

Результат: на изменённой поверхности узлы и исходящие `CALLS` свежие; входящие `CALLS` от
неизменённых вызывающих сохранены.

### 5.3 Новые методы `GraphStore`

- `symbols_for_paths(repo, paths) -> set[str]`:
  `MATCH (s:Symbol{repo:$repo}) WHERE any(p IN $prefixes WHERE s.id STARTS WITH p) RETURN s.id`,
  `prefixes = [p + "#" for p in paths]`.
- `delete_symbols(repo, ids)`:
  `UNWIND $ids AS id MATCH (s:Symbol{repo:$repo, id:id}) DETACH DELETE s`.
- `delete_outgoing_calls(repo, ids)`:
  `UNWIND $ids AS id MATCH (s:Symbol{repo:$repo, id:id})-[r:CALLS]->() DELETE r`.

### 5.4 Лимиты (честно, для README)

Инкрементальный патч НЕ восстанавливает:
- рёбра `IMPLEMENTS` (tree-sitter их не строит — только SCIP);
- исходящие `CALLS` из изменённых файлов в **неизменённые** (цель вне переданного набора);
- новые входящие `CALLS` от **неизменённых** вызывающих в **новый** символ (вызывающий не
  переразбирается).

Полная точность (IMPLEMENTS + все рёбра) восстанавливается на следующем ручном `reviewer index`
с SCIP. SCIP-путь остаётся только в `reviewer index` (требует локальный worktree, недоступный
MCP-серверу).

### 5.5 Fail-soft

Сбой патча графа логируется `log.warning` и **не валит** prepare (дрейф графа не критичен; ровно как
текущий self-heal векторов в `try/except`).

## 6. Порядок сборки (фазы)

1. **Storage repo-discriminator (F1, ядро):** схема Postgres + `ChunkStore` + `freshness` + схема
   Neo4j + `GraphStore` (без новых F2-методов). Юнит-тесты изоляции.
2. **Проброс repo (F1, верх):** `Retriever`, `ToolContext`/`make_tools`, `review_service.prepare`,
   `MCPReviewService`, `search_codebase`, граф задач (`TOUCHES` repo-aware), CLI `--repo` + derive,
   `Settings.default_repo`.
3. **Авто-реиндекс графа (F2):** новые `GraphStore`-методы + патч в блоке дрейфа `prepare`.
4. **Доки:** оба README (правка пунктов «no graph auto-reindex» и «single-repo» в Known
   limitations + раздел про `--repo`/`DEFAULT_REPO`), `.env.example`, CLAUDE.md инварианты.

Каждая фаза — самостоятельный зелёный прогон тестов.

## 7. Тестирование

- **Unit (на фейках, без внешних API):**
  - изоляция: чанки/символы двух репо не видят друг друга (`hybrid_search`, `fetch_nodes`, `expand`,
    `callers`, `find_symbol` фильтруют по repo);
  - `freshness` пишет `ChunkRow.repo`; overlay `pr:N` двух репо не сталкиваются;
  - F2: `symbols_for_paths`/`delete_symbols`/`delete_outgoing_calls`; патч сохраняет входящие CALLS,
    удаляет stale-символы, обновляет исходящие;
  - repo identity: `normalize_repo` (регистр) + derive из разных форм remote-URL (ssh/https/.git/без
    remote → ошибка).
- **Integration (маркер `integration`):** реальный round-trip Postgres/Neo4j на двух репо —
  изоляция и инкрементальный патч графа.

## 8. Обновления документации

- `README.md` / `README.ru.md`: убрать/переписать пункты «No graph auto-reindex» и «Single-repo» в
  «Known limitations»; добавить `reviewer index --repo`, `DEFAULT_REPO`, описание инкрементального
  self-heal графа и его лимитов.
- `.env.example`: `DEFAULT_REPO`.
- `CLAUDE.md`: обновить инварианты (single-repo → multi-repo discriminator; граф самолечится
  инкрементально).

## 9. Вне scope (YAGNI)

- Кросс-репо ретрив/поиск (решение #4 — нет).
- SCIP в горячем пути prepare (требует worktree/клон — решение #1 = tree-sitter).
- Webhook/GitHub App автозапуск ревью (отдельная фича, не трогаем).
- Полный инкрементальный SCIP-граф (точность восстанавливается ручным `reviewer index`).

## 10. Риски

- **Composite uniqueness в Neo4j Community.** План — property-uniqueness `(repo, id)` (поддержано в
  5.x Community). Если в целевой версии недоступно — fallback на composite INDEX + опора на
  MERGE-по-двум-свойствам (уникальность гарантируется семантикой MERGE). Проверить на этапе фазы 1.
- **Миграция существующих деплоев.** Forward-only схема + бэкфилл по `DEFAULT_REPO` или одноразовый
  `reviewer index --repo`. Без `DEFAULT_REPO` и без реиндекса старые строки с `repo IS NULL` не
  попадут в выдачу (фильтр по repo) — это ожидаемо и документируется.
- **Поверхность изменений велика** (store/graph/retriever/tools/mcp/service/cli). Митигируется
  фазовым порядком и тестами изоляции на каждой фазе.
