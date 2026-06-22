# Компактный вывод графовых тулов (find_callers / get_related_symbols) — дизайн

**Задача:** PRI-148 (ID-148) · Слой: движок (MCP-тулы агента) · Оценка: S–M
**Прецедент:** PRI-138 (PR #23) — то же причёсывание выдачи для `search_codebase`.

## Проблема

`find_callers` и `get_related_symbols` возвращают сырые `node_id` (`path#fqn`),
склеенные через `"\n".join(sorted(...))`. Агент вынужден делать follow-up `read_file`
на каждый символ, чтобы понять контекст → лишние токены и шаги. Тип связи
(CALLS / IMPLEMENTS / TESTED_BY) и местоположение (`file:line`) не видны.

Текущие точки:
- `reviewer/tools/code_tools.py:67-70` — `get_related_symbols` → `graph.expand(...)`.
- `reviewer/tools/code_tools.py:114-119` — `find_callers` → `graph.callers(...)`.
- `reviewer/mcp/service.py:411-426` — session-less `related_symbols` → `graph.expand(...)`.
- `reviewer/mcp/service.py:428-443` — session-less `callers` → `graph.callers(...)`.

## Цель

На каждый элемент выдачи обоих тулов (в обоих контурах — сессионном PR-ревью и
session-less для `solve-task`/`ask`):

```
// {node_id} ({path}:{start_line}) [{rel}]
{первая непустая строка text — сигнатура/строка определения}
```

**Критерий приёмки:** выдача содержит `file:line` + краткий контекст + тип связи;
число follow-up `read_file` на типовом PR падает.

## Решения (зафиксированы в брейншторме)

1. **Оба контура** приводятся к новому формату через единый форматтер — паритет
   `review-pr` / `solve-task` / `ask`.
2. **Сниппет для `find_callers`** — строка определения вызывающего символа (через
   `store.fetch_nodes`), **без** пересборки графа. Точная строка call-site (захват при
   построении графа) — осознанно отложенный, более тяжёлый путь (трогает оба бэкенда
   графа + реиндекс).
3. **Подход A** — обогащение в слое тулов/сервиса через общий форматтер + новые
   detailed-методы графа. Сигнатуры `expand`/`callers` (`set[str]`) **не меняем** —
   их держат ретрив и impact.

## Архитектура

### 1. Граф-стор: новые detailed-методы (аддитивно)

`reviewer/graph/store.py`. Старые `expand`/`callers` остаются как есть (их потребляют
`retriever.py:92,128` и `impact.py:84` — им нужен голый `set[str]`).

**`callers_detailed(repo, node_ids, *, branch="") -> list[dict]`**
```cypher
UNWIND $ids AS sid
MATCH (c:Symbol {repo: $repo, branch: $branch})-[:CALLS]->(s:Symbol {repo: $repo, branch: $branch, id: sid})
RETURN DISTINCT c.id AS id
ORDER BY id
```
→ `[{"id": <c.id>, "rel": "CALLS"}]`. Тип ребра всегда `CALLS` (направленные входящие).

**`expand_detailed(repo, node_ids, hops=2, *, branch="") -> list[dict]`**
```cypher
UNWIND $ids AS sid
MATCH (s:Symbol {repo: $repo, branch: $branch, id: sid})
MATCH p=(s)-[:CALLS|IMPLEMENTS|TESTED_BY*1..$hops]-(n:Symbol {repo: $repo, branch: $branch})
WHERE n.id <> sid
WITH n.id AS id, [r IN relationships(p) | type(r)] AS rels, length(p) AS dist
ORDER BY dist
WITH id, collect({rels: rels, dist: dist})[0] AS best
RETURN id, best.rels AS rels, best.dist AS dist
ORDER BY best.dist, id
```
→ `[{"id", "rels": ["CALLS"], "dist": 1}]`. На каждого соседа берётся **кратчайший**
путь и типы рёбер вдоль него. Фильтр `n.id <> sid` — не показываем сам символ как
своего соседа (осознанное уточнение vs старый `expand`, который мог вернуть seed через
цикл).

### 2. Общий форматтер

Новый модуль `reviewer/tools/graph_format.py` — единственный источник правды рендера
для обоих контуров.

```python
def format_neighbors(neighbors, *, store, repo, branch, overlay_ref, changed_paths, empty_msg) -> str
```

- `neighbors` — список `{"id", "rel"?, "rels"?, "dist"?}` из detailed-методов.
- Пустой вход → `empty_msg`.
- Один батч-запрос `store.fetch_nodes(repo, ids, overlay_ref, changed_paths, base_ref=base_ref(branch))`
  → словарь `node_id -> Retrieved` (несёт `path`, `start_line`, `text`).
- Рендер элемента:
  `// {id} ({path}:{start_line}) [{rel}]` + перевод строки + первая непустая строка `text`.
- Метка `rel`:
  - `callers` → `[CALLS]`.
  - `related` → типы кратчайшего пути + дистанция: `[CALLS, d1]`, `[IMPLEMENTS, d1]`,
    `[CALLS→TESTED_BY, d2]`.
- Дегрейд:
  - id отсутствует в индексе (`fetch_nodes` не вернул) → `// {id} [{rel}] (вне индекса)`
    — символ не теряем.
  - `store is None` → сырой список id (старое поведение, не падаем).
- Порядок **сохраняется из detailed-метода** (детерминированный `ORDER BY` в Cypher:
  `callers` по `id`, `related` по `dist`); форматтер не пересортировывает. **Кап 25**
  элементов (как `find_symbol LIMIT 25`) применяется поверх этого порядка; при усечении
  хвостом — `(…ещё N, усечено)`.

### 3. Развод по тулам (оба контура)

**`reviewer/tools/code_tools.py`** (сессионные):
- `get_related_symbols` → `ctx.graph.expand_detailed(ctx.repo, [node_id], hops=2, branch=ctx.branch)`
  → `format_neighbors(..., store=ctx.store, overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths, empty_msg="(нет связей)")`.
- `find_callers` → `ctx.graph.callers_detailed(...)`
  → `format_neighbors(..., empty_msg="(вызовов не найдено)")`.
- Существующие гарды (`graph is None` / нет метода) сохраняем.

**`reviewer/mcp/service.py`** (session-less):
- `related_symbols` / `callers` — то же, `overlay_ref=None`, `changed_paths=[]`,
  `store=self.components.store`, `base_ref(resolved)`. Существующие try/except сохраняем.

**`reviewer/entrypoints/mcp_server.py`**: докстринги тулов `get_related_symbols`,
`find_callers`, `related_symbols`, `callers` обновить под новый контракт
(file:line + сниппет + тип ребра).

### 4. Поток данных

```
tool(node_id)
  → graph.{expand,callers}_detailed   (Neo4j: id + rel/rels + dist)
  → format_neighbors
      → store.fetch_nodes             (Postgres: path / start_line / text)
  → форматированная строка
```

Граф даёт связи и типы рёбер, Postgres — строки/сниппеты; джойн по `node_id`
(инвариант `node_id = "path#fqn"`).

## Обработка ошибок (fail-open)

- Сбой графа → существующие `(нет связей)` / `(вызовов не найдено)` / `(граф недоступен)`.
- Частичный промах `fetch_nodes` → строки `(вне индекса)`, не падаем.
- `store is None` → сырые id (деградация к старому поведению).

## Тестирование

- **Unit** (фейк-стор, без Neo4j): `format_neighbors` — формат
  `// id (path:line) [REL]\n<сниппет>`; пустой вход → `empty_msg`; id вне индекса →
  `(вне индекса)`; `store=None` → сырые id; кап/усечение `(…ещё N, усечено)`.
- **Integration** (Neo4j, маркер `integration`) в `tests/graph/test_store.py`:
  `expand_detailed` / `callers_detailed` — типы рёбер CALLS/IMPLEMENTS/TESTED_BY,
  дистанция, фильтр self (`n.id <> sid`).
- **Обновить ожидания** существующих тестов под новый формат:
  `tests/tools/test_code_tools.py`, `tests/mcp/test_service.py`,
  `tests/mcp/test_server_tools.py`.
- **Не трогаем** `tests/tools/test_impact.py` (использует `callers()` → `set[str]`,
  сигнатура не меняется).

## Затронутые файлы

| Файл | Изменение |
|---|---|
| `reviewer/graph/store.py` | + `callers_detailed`, `expand_detailed` (старые методы не трогаем) |
| `reviewer/tools/graph_format.py` | новый — `format_neighbors` |
| `reviewer/tools/code_tools.py` | `get_related_symbols`, `find_callers` → detailed + форматтер |
| `reviewer/mcp/service.py` | session-less `related_symbols`, `callers` → detailed + форматтер |
| `reviewer/entrypoints/mcp_server.py` | докстринги 4 тулов |
| `tests/graph/test_store.py` | + integration-тесты detailed-методов |
| `tests/tools/test_code_tools.py`, `tests/mcp/test_service.py`, `tests/mcp/test_server_tools.py` | обновить ожидания формата |
| `tests/tools/test_graph_format.py` | новый — unit форматтера |

## Вне объёма (отложено)

- Точная строка call-site для `find_callers` (захват номера строки вызова при
  построении графа — оба бэкенда + реиндекс).
- Изменение `expand`/`callers` (сигнатуры переиспользуются ретривом/impact).
