# PRI-167 — Сводки подсистем (A): векторизация сводок + отбор по близости (top-k при масштабе)

**Дата:** 2026-06-23
**Задача:** PRI-167 (канонический ключ ID-167), третья из трёх (C → B → **A**). Зависимости B (PRI-166, depth-in-env) и C (PRI-165, `source_hash`) уже в коде. Потребитель — PRI-161 (приор сводок в solve-task).
**Оценка:** M.

## Проблема

`get_subsystem_summaries` отдаёт **все** сводки репо/ветки (`reviewer/index/summary_store.py:93`). На ~15 кластерах это дёшево и даже полезно (полная карта), но на сотнях подсистем (прод-монорепо) раздувает приор и шумит. Колонка `embedding vector(1024)` в таблице `subsystem_summaries` есть (`schema.sql:83`), но `upsert_summary` её **не заполняет**, а отбор не векторный. Цель — сделать слой сводок «настоящим GraphRAG»: отбор сообщества по близости (top-k), а не «отдать все».

## Цель и критерии приёмки

- При числе сводок **больше порога** запрос с `query` возвращает **top-k** релевантных подсистем по близости.
- При **≤ порога** (или без `query`) возвращаются **все** — бэк-компат.
- Эмбеддинги сводок **дедуплицируются по `source_hash`** — нет лишних вызовов Voyage на неизменных сводках.
- **Recall:** для архитектурного вопроса top-k содержит правильную подсистему.

## Принятые решения (brainstorming)

1. **Поиск — чистый ANN (cosine)**, не гибрид. Сводки короткие, запрос — архитектурный вопрос; семантики достаточно. Не требует BM25-индекса на `subsystem_summaries` и `_bm25_query`.
2. **Порог масштаба — env-дефолт + per-repo `.review.yml` override**, единообразно с `summary_cluster_depth` (PRI-166). `SUMMARY_TOPK_THRESHOLD`, дефолт **20**.
3. **Бэкфилл эмбеддингов — серверный self-heal в скилле.** `summarize-subsystems` после LLM-прохода дёргает серверный шаг, эмбедящий все строки с `embedding IS NULL` из хранимого `title+summary` (без LLM, дедуп по `source_hash`). Индекс самозалечивается.
4. **`top_k` при `query` по умолчанию — 8** (приор дешёвый текстом, держим recall высоким).

## Архитектура

### 1. Схема и миграция — `reviewer/index/schema.sql`

Колонка `embedding vector(1024)` уже есть (nullable). Добавить HNSW-индекс (зеркало `chunks_hnsw`, `schema.sql:36`):

```sql
CREATE INDEX IF NOT EXISTS subsystem_summaries_hnsw ON subsystem_summaries
USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

**Миграция:** `ChunkStore.init_schema()` идемпотентен (`CREATE INDEX IF NOT EXISTS`) → индекс накатывается на следующем `reviewer index` / старте сервера. NULL-эмбеддинги HNSW не индексирует — это корректно (строки без вектора в ANN не участвуют, бэкфилл их позже заполнит).

### 2. Слой хранения — `reviewer/index/summary_store.py`

- `upsert_summary(...)` — добавить параметр `embedding: list[float] | None = None`; писать его в `INSERT` и в `ON CONFLICT … DO UPDATE SET`.
- `search_summaries(repo, branch, query_embedding, top_k) -> list[dict]` — чистый ANN: `WHERE repo=%s AND branch=%s AND embedding IS NOT NULL ORDER BY embedding <=> %(vec)s LIMIT k`. Возвращает тот же shape, что `get_summaries` (`cluster_key, title, summary, updated_at`). Fail-soft на `UndefinedTable` → `[]`.
- `count_summaries(repo, branch) -> int` — `SELECT COUNT(*)` для проверки порога. Fail-soft → `0`.
- Бэкфилл: `get_pending_embeddings(repo, branch) -> list[dict]` (строки с `embedding IS NULL`: `cluster_key, title, summary`) + `set_embedding(repo, branch, cluster_key, embedding)` (или батч-`set_embeddings`). Используется `backfill_summary_embeddings`.
- `get_summaries` остаётся без изменений (no-query / below-threshold путь — бэк-компат).

`register_vector` уже сконфигурирован на пуле (`summary_store.py:30`), поэтому передача/чтение `Vector` работает без доработок.

### 3. Индексный путь + self-heal — `reviewer/mcp/service.py`

- `index_subsystem_summary(...)`: после резолва `member_node_ids` вычислить эмбеддинг `f"{title}\n{summary}"` через `self.components.embedder.embed_documents([...])[0]`. **Дедуп по `source_hash`:** прочитать существующую строку (`summary_store.get_summary`); если её `source_hash` совпадает с переданным **и** `embedding` ненулевой — переиспользовать (Voyage не дёргать). Иначе эмбедить. Передать `embedding` в `upsert_summary`. Паттерн — `TaskService.index_batch` (`reviewer/tasks/service.py:136`, эмбедит только `to_embed`). Fail-soft: сбой Voyage → `embedding=None` + note (сводка всё равно сохраняется, бэкфилл доберёт).
- Новый серверный метод/тул `backfill_summary_embeddings(repo, branch=None) -> dict`: взять строки с `embedding IS NULL`, батч-эмбедить из хранимого `title+summary` (`embed_documents`, батч 128), записать. Идемпотентен (следующий прогон находит 0). Возвращает `{embedded: N}` (+ `note` при недоступности/пустоте). Fail-soft.

### 4. Поисковый путь — `get_subsystem_summaries`

Сигнатура → `get_subsystem_summaries(repo, branch=None, cluster_key=None, query=None, top_k=None)`.

Логика:
- `cluster_key` задан → как сейчас (один summary через `get_summary`).
- иначе, если `query` задан **и** `count_summaries(repo, branch) > threshold` → `embed_query(query)` + `search_summaries(..., top_k or 8)` → top-k.
- иначе → `get_summaries(repo, branch)` (все). Покрывает no-query **и** «≤ порога» — бэк-компат.

Порог резолвится `_resolve_summary_topk_threshold(repo, branch)` — зеркало `_resolve_summary_depth` (`service.py:331`).

### 5. Конфиг порога

- `reviewer/config/settings.py`: `summary_topk_threshold: int = 20` (env `SUMMARY_TOPK_THRESHOLD`), рядом с `summary_cluster_depth:71`.
- `reviewer/policy/policy.py`: поле `summary_topk_threshold` + чтение в `from_settings` (дефолт из settings) + override из `.review.yml` в `load` (зеркало `summary_cluster_depth`, `policy.py:41/55/85`).
- `reviewer/mcp/service.py`: `_resolve_summary_topk_threshold(repo, branch) -> tuple[int, str]` (как `_resolve_summary_depth`).

### 6. Потребители (скиллы и MCP-тулы)

- `plugin/skills/ask/SKILL.md:46` — `get_subsystem_summaries(repo, branch, query="<question>")`.
- `plugin/skills/pr-walkthrough/SKILL.md:38` — `get_subsystem_summaries(repo, pr.base_ref, query="<PR title / changed paths>")`.
- `plugin/skills/summarize-subsystems/SKILL.md` — после LLM-прохода и `prune` вызывать `backfill_summary_embeddings(repo, branch)`.
- `reviewer/entrypoints/mcp_server.py:213` — расширить сигнатуру/докстринг `get_subsystem_summaries` (`query`, `top_k`); зарегистрировать тул `backfill_summary_embeddings`.

### 7. Тесты

`tests/mcp/test_subsystem_summaries.py` — расширить:
- index пишет `embedding` (не NULL после `index_subsystem_summary`);
- дедуп по `source_hash`: повторный index с тем же hash **не зовёт** embedder (фейк-embedder со счётчиком вызовов);
- `query` выше порога → top-k по близости, в top-k правильная подсистема (recall);
- `query` ≤ порога **и** no-query → все (бэк-компат);
- `backfill_summary_embeddings` заполняет NULL-эмбеддинги, повторный вызов → 0.

Guard-тесты:
- `tests/mcp/test_server.py:126` — обновить список тулов (добавить `backfill_summary_embeddings`).
- `tests/skills/test_ask_uses_summaries.py` — под новую сигнатуру `get_subsystem_summaries(..., query=...)`.

### 8. Бэк-компат и инварианты

- No-query путь не меняется; `tests/mcp/test_subsystem_summaries.py:86` (вызов без `query`) продолжает отдавать все.
- Voyage-экономия (free tier 3 RPM / 10K TPM): дедуп по `source_hash` (index) + LRU query-кэш (`embeddings.py:49`) + бэкфилл эмбедит только NULL.
- Язык проекта — русский (комментарии/докстринги/CLI). Conventional Commits на русском, без self-attribution.

## Вне области (YAGNI)

- Гибридный (BM25⊕ANN) поиск по сводкам — отвергнут (см. решение 1).
- Реранкинг сводок (Voyage rerank) — приор и так дешёвый/короткий, не нужен.
- Per-call отключение порога/принудительный top-k мимо порога — не требуется критериями.
