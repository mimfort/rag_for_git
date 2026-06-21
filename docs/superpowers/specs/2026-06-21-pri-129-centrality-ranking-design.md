# PRI-129 (ID-129) — Ранжирование находок по центральности в графе

**Доска:** Движок (reviewer CLI/MCP) · **Оценка:** S–M · **Зависимость:** граф кода (Neo4j)
**Статус спеки:** утверждена в brainstorming (2026-06-21)

## Цель

При **равной severity** находка, расположенная в высокоцентральном символе (много входящих
`CALLS` — «хаб», от которого зависит много кода), должна:

- идти **выше** при сортировке находок;
- **реже отсекаться** cap'ом `max_comments` (т.е. чаще публиковаться inline, а не уходить в сводку).

Это **дословно** критерий приёмки карточки.

## Что это НЕ затрагивает

Существующий **Voyage-реранкер ретрива** (`reviewer/retrieval/`, `reviewer/index/reranker.py`)
переранжирует **извлечённые чанки** (вход-контекст агента, векторный/гибридный поиск). PRI-129 —
**ортогональный** механизм: новый сигнал приоритета для **выходных находок (findings)** из **графа
кода**, на этапе `assemble`/cap, уже после LLM. Эти два механизма не пересекаются.

## Ключевое продуктовое решение

Центральность — **tie-breaker внутри severity-группы**, не агрессивный множитель:

- `severity` остаётся **первичным** ключом сортировки — центральность никогда не поднимает `low`
  выше `high`;
- центральность — **вторичный** ключ (перед `confidence`);
- сырой целочисленный degree используется как ключ напрямую — **нормализация и подбор веса не
  нужны** (отвергнуты как избыточные, YAGNI).

## Архитектура изменений

Три точки, каждая с одной ответственностью:

### 1. Метрика центральности — `reviewer/graph/store.py`

Новый метод-сосед `GraphStore.callers` (тот же MATCH-паттерн, но агрегат):

```python
def in_degree(self, repo, node_ids, *, branch="") -> dict[str, int]:
    """Число входящих CALLS на символ (центральность = сколько мест зависит от него).

    Узлы без вызывающих в словарь не попадают → вызывающий трактует отсутствие как 0.
    """
```

Cypher (зеркало `callers`, строки 86–94, с `count`):

```cypher
UNWIND $ids AS sid
MATCH (c:Symbol {repo: $repo, branch: $branch})-[:CALLS]->
      (s:Symbol {repo: $repo, branch: $branch, id: sid})
RETURN sid AS id, count(c) AS deg
```

**Семантика degree:** **входящие** `CALLS` (не in+out). Карточка: «число входящих CALLS». Замечание:
рёбра `CALLS` создаются через `MERGE` (`store.py:73`) → на пару (caller→callee) ровно одно ребро,
поэтому «число входящих CALLS-рёбер» и «число уникальных вызывающих» **тождественны** в этом графе.

### 2. Маппинг `Finding → символ` и проводка — `reviewer/mcp/service.py::publish_review`

`Finding` несёт только `file`/`line`, **без** `fqn`/`node_id`. Центральность считается в
`publish_review` **между dedup и assemble** (после грунтовки строки, ~строка 556), по образцу
`reviewer/tools/impact.py::compute_impact`:

1. `nodes = store.fetch_nodes_at(repo, p.changed_node_ids, overlay_ref)` — символы изменённых файлов
   с диапазонами `start_line`/`end_line` (узел `Retrieved` несёт оба поля).
2. Для каждого `Finding`: по `(file, grounded line)` найти **охватывающий** символ
   (`path == file and start_line <= line <= end_line`). При вложенности (метод внутри класса) —
   брать символ с **самым узким** диапазоном.
3. `deg = graph.in_degree(repo, matched_node_ids, branch=branch)` — **один** батч-запрос на весь
   `publish`.
4. Проставить `f.centrality = float(deg.get(nid, 0))`.

Маппинг использует **уже грунтованную** `f.line` (после `ground_line` + `snap_to_commentable`,
строки 547–551). Последующая мутация `f.line` через `_sane_line` внутри `assemble` — лишь уточнение
координаты, символ при этом практически не меняется; допустимо.

**Стоимость:** +1 запрос Postgres (часто данные уже под рукой) + 1 запрос Neo4j на весь `publish`,
**0 обращений к Voyage**. Эффективно O(1).

### 3. Сортировка, модель данных, fail-soft — `reviewer/vcs/base.py` + `reviewer/agent/assemble.py`

**Модель** (`reviewer/vcs/base.py::Finding`, строки 31–49):

- новое поле `centrality: float = 0.0` (дефолт 0);
- **не** включается в `fingerprint()` → идемпотентность комментариев не меняется.

**Сортировка** (`reviewer/agent/assemble.py:267–270`):

```python
# было
key=lambda f: (-_SEVERITY_RANK.get(f.severity, 0), -f.confidence)
# стало
key=lambda f: (-_SEVERITY_RANK.get(f.severity, 0), -f.centrality, -f.confidence)
```

Cap по `max_comments` (строки 286–294) **не трогаем** — высокоцентральные находки уже выше в своей
severity-группе, поэтому при срабатывании cap'а первыми в сводку уходят **менее** центральные той же
severity. Критерий приёмки удовлетворяется автоматически.

**Fail-soft:** `graph is None`, символ не пойман маппингом, Neo4j недоступен → `centrality = 0.0` для
всех находок → ключ сортировки вырождается в текущий `(severity, confidence)`. Ноль нейтрален, ревью
не падает (тот же контракт, что `compute_impact` → `[]`).

## Границы (что НЕ делаем)

- `reviewer/policy/policy.py::gate` (строки 84–95) **не трогаем** — это булев фильтр
  (severity/confidence/category/ignore), множитель в него не ложится. Карточка упоминала
  `policy.py` ИЛИ `assemble.py` — берём `assemble.py`.
- Без env-флагов, конфигов, весов и нормализации — сырой degree как tie-breaker.
- Без изменения cap-логики и формата комментариев.

## Тестирование

- **Регресс:** 5 существующих тестов `tests/agent/test_assemble.py` строят `Finding` без
  `centrality` → дефолт 0 → порядок не меняется → проходят без правок.
- **Новые unit:**
  - `in_degree` (граф-тест, маркер `integration` или на фейк-графе по образцу существующих) —
    счётчик входящих CALLS, узел без вызывающих → отсутствует в словаре (→ 0 у вызывающего).
  - Маппинг line-in-range — попадание в символ, вложенность (выбор узкого диапазона), промах
    (строка вне всех символов → 0).
  - `assemble` — при равной severity находка с большей `centrality` идёт раньше и переживает cap,
    тогда как менее центральная той же severity уходит в сводку.

## Затронутые файлы

| Файл | Изменение |
|---|---|
| `reviewer/graph/store.py` | + метод `in_degree` |
| `reviewer/vcs/base.py` | + поле `Finding.centrality: float = 0.0` |
| `reviewer/mcp/service.py` | в `publish_review`: маппинг finding→символ + `in_degree` + проставление `centrality` |
| `reviewer/agent/assemble.py` | третичный ключ сортировки `-f.centrality` |
| `tests/graph/…`, `tests/mcp/…`, `tests/agent/test_assemble.py` | новые unit-тесты |

## Переиспользование

`reviewer/tools/impact.py::compute_impact` (PRI-126, смержен в `dev`) — готовый образец: уже
использует `graph.callers(repo, [nid], branch=branch)` и `store.fetch_nodes_at(repo, node_ids, ref)`
(узел → `path`/`start_line`/`text`). Маппинг finding→символ и запрос degree повторяют этот паттерн.
