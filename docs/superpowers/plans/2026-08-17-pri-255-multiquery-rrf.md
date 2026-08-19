# PRI-255 Multi-query с RRF-слиянием — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** секция `code` контекста задачи ищется набором подзапросов из структуры и сущностей задачи, выдачи сливаются RRF, и рендер перестаёт выжигаться одним большим чанком.

**Architecture:** рядом с `Retriever.search_base` (не меняется) появляется параллельный путь `search_multi`: N подзапросов эмбеддятся одним батчем, N раз зовётся `store.hybrid_search` (только Postgres), выдачи сливаются формулой `Σ 1/(60+rank)`, дальше один graph-expand, дедуп, потолок и обрезка блоков по границе строк. Реранка и cliff в этом пути нет — финальный ранкер RRF. Включается только внутри `prepare_task_context`; публичный тул `search_codebase` не меняется.

**Tech Stack:** Python 3.12, pytest, psycopg/pgvector (ParadeDB), Neo4j, Voyage (embeddings), argparse-харнесс `eval/solve_task_metrics`.

**Spec:** `docs/superpowers/specs/2026-08-17-pri-255-multiquery-rrf-design.md`

## Global Constraints

- Язык кода, комментариев, докстрингов и сообщений — **русский**.
- Коммиты — Conventional Commits по-русски, **без self-attribution** (никаких `Co-Authored-By`, упоминаний Claude).
- TDD: сначала падающий тест, потом минимальная реализация.
- Unit-тесты — **без Postgres, Neo4j, localhost-сокетов и внешней сети**. Прогон: `.venv/bin/pytest -q`.
- Любой тест с реальной сетью обязан иметь `@pytest.mark.integration`.
- `Retriever.search_base` (`reviewer/retrieval/retriever.py:152-226`) **не менять ни на строку**.
- Публичный MCP-тул `search_codebase` (`reviewer/mcp/service.py:1769-1795`) **не менять**.
- `ContextPack.as_context` (`reviewer/retrieval/retriever.py:94-120`) **не менять** — он общий с ревью PR.
- Константа RRF — `60`, форма `Σ 1/(60+rank)`, как в `store.hybrid_search:495-499`. Третьей формулы RRF в проекте не появляется.
- `MAX_SUBQUERIES = 20`, `MAX_BLOCK_CHARS = 2000`, `RRF_K = 60`.
- Ноль LLM-вызовов в новом пути; один вызов Voyage на сборку секции.
- `git push`, создание PR и любая запись в доску требуют явного подтверждения пользователя.

---

## File Structure

| Файл | Ответственность |
|---|---|
| `reviewer/mcp/subqueries.py` (создать) | чистое извлечение подзапросов из текста задачи |
| `reviewer/retrieval/multiquery.py` (создать) | RRF-слияние, обрезка блока, оркестрация `search_multi` |
| `reviewer/index/embeddings.py` (изменить) | `embed_queries` — батч запросов через тот же LRU-кэш |
| `reviewer/mcp/task_context.py` (изменить) | `_queries` / `_test_queries`; передача списка в `deps` |
| `reviewer/mcp/service.py` (изменить) | `_search_codebase_multi`; `_TaskContextDeps.code`/`.test_exemplars` |
| `eval/solve_task_metrics/live.py` (изменить) | `code_multi` — тот же продакшн-путь |
| `eval/solve_task_metrics/variants.py` (изменить) | вариант `multiquery` |
| `eval/solve_task_metrics/subquery_stats.py` (создать) | распределение числа подзапросов по корпусу |
| `eval/solve_task_metrics/__main__.py` (изменить) | подкоманда `subqueries` |

---

### Task 1: Чистый модуль извлечения подзапросов

**Files:**
- Create: `reviewer/mcp/subqueries.py`
- Test: `tests/mcp/test_subqueries.py`

**Interfaces:**
- Consumes: ничего (модуль без зависимостей проекта).
- Produces: `MAX_SUBQUERIES: int = 20`; `build_subqueries(task: dict | None, base_query: str) -> list[str]` — первый элемент всегда `base_query`; длина ≤ `MAX_SUBQUERIES`.

**Почему `base_query` приходит аргументом, а не берётся из `task_context`:** `task_context.py` будет импортировать этот модуль, обратный импорт `_query` замкнул бы цикл. Модуль остаётся чистым и тестируется на текстах задач.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/mcp/test_subqueries.py`:

```python
"""Извлечение подзапросов ретрива из структуры и сущностей задачи (PRI-255)."""
from reviewer.mcp.subqueries import MAX_SUBQUERIES, build_subqueries

SMALL = {
    "title": "Починить дрейф индекса",
    "description": "## Проблема\n\nСтатус врёт про дрейф.\n",
}

BULK = {
    "title": "Реестр досок",
    "description": (
        "## Проблема\n\nПровайдеры ветвятся по типу.\n\n"
        "## Что сделать\n\n"
        "1. Завести BoardProviderRegistry в reviewer/tasks/registry.py.\n"
        "2. Перенести yougile на RestBoardBase.\n"
        "3. Перенести youtrack на общий транспорт.\n"
        "4. Добавить пагинацию по Link-заголовку.\n"
        "5. Вымарывать секреты на границе.\n"
        "6. Описать матрицу в docs/board-providers.md.\n"
        "7. Закрыть contract-фикстурой каждый тип.\n"
        "8. Прокинуть provider_options до фабрики.\n"
        "9. Синхронизировать таблицу tasks с новым ключом.\n"
        "10. Обновить sync_board на generic lifecycle.\n"
    ),
}


def test_base_query_is_always_first():
    assert build_subqueries(SMALL, "база")[0] == "база"


def test_board_less_input_yields_only_base_query():
    assert build_subqueries(None, "свободная формулировка") == ["свободная формулировка"]


def test_small_task_yields_few_subqueries():
    assert len(build_subqueries(SMALL, "база")) <= 3


def test_bulk_task_yields_one_subquery_per_item():
    queries = build_subqueries(BULK, "база")
    assert 10 <= len(queries) <= 15
    assert any("BoardProviderRegistry" in q for q in queries)
    assert any("generic lifecycle" in q for q in queries), "хвостовой пункт обязан попасть"


def test_identifiers_are_bundled_into_one_subquery():
    task = {
        "title": "T",
        "description": "Правится search_codebase и TaskStore в reviewer/mcp/service.py, таблица tasks.",
    }
    queries = build_subqueries(task, "база")
    bundle = queries[-1]
    for identifier in ("search_codebase", "TaskStore", "reviewer/mcp/service.py"):
        assert identifier in bundle
    assert " и " not in bundle, "предлоги в пул идентификаторов не попадают"


def test_criteria_items_become_subqueries():
    task = {"title": "T", "description": "D", "criteria": ["Метрика растёт", "Отчёт зафиксирован"]}
    queries = build_subqueries(task, "база")
    assert "Метрика растёт" in queries


def test_duplicates_are_dropped_preserving_order():
    task = {"title": "T", "description": "## Что сделать\n\n1. база\n2. другое\n"}
    assert build_subqueries(task, "база").count("база") == 1


def test_degenerate_text_is_capped():
    items = "\n".join(f"{i}. пункт номер {i} про symbol_{i}" for i in range(1, 61))
    task = {"title": "T", "description": f"## Что сделать\n\n{items}\n"}
    assert len(build_subqueries(task, "база")) == MAX_SUBQUERIES
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/mcp/test_subqueries.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.mcp.subqueries'`

- [ ] **Step 3: Написать минимальную реализацию**

Создать `reviewer/mcp/subqueries.py`:

```python
"""Подзапросы ретрива из структуры и сущностей задачи (PRI-255).

Один эмбеддинг на весь текст задачи не близок ни к одной из её тем, а
хвостовые пункты «Что сделать» не ищутся вовсе. Модуль детерминированно
разбирает текст на подзапросы: по пункту списка требований — свой запрос,
плюс один пул технических идентификаторов из прозаической части.

Ни сети, ни БД, ни LLM: расход токенов от этого модуля не растёт, а его
поведение проверяется на текстах задач.
"""
from __future__ import annotations

import re

MAX_SUBQUERIES = 20
"""Предохранитель против вырожденного текста.

Число подзапросов производно от размера задачи (пункты требований), но
текст из шестидесяти пунктов не должен превращаться в шестьдесят обращений
к хранилищу.
"""

MAX_IDENTIFIERS = 12
"""Сколько идентификаторов попадает в пул. Дальше запрос теряет фокус."""

MIN_IDENTIFIER_LEN = 4

_SECTION_RE = re.compile(r"(?i)^#{1,6}\s*.*(что сделать|критери|приёмк|acceptance)")
_HEADING_RE = re.compile(r"^#{1,6}\s")
_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./]*")


def _is_identifier(token: str) -> bool:
    """Технический идентификатор: snake_case, CamelCase, путь или файл .py."""
    if len(token) < MIN_IDENTIFIER_LEN:
        return False
    if "/" in token or token.endswith(".py"):
        return True
    if "_" in token:
        return True
    return token[:1].isupper() and any(c.isupper() for c in token[1:])


def _items(text: str) -> list[str]:
    """Пункты списков под заголовками требований и критериев приёмки."""
    items: list[str] = []
    in_section = False
    for line in text.splitlines():
        if _HEADING_RE.match(line):
            in_section = bool(_SECTION_RE.match(line))
            continue
        if not in_section:
            continue
        match = _ITEM_RE.match(line)
        if match:
            items.append(match.group(1))
    return items


def _identifiers(text: str, items: list[str]) -> list[str]:
    """Идентификаторы прозаической части: то, что не ушло в пункты списков."""
    consumed = set(items)
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        match = _ITEM_RE.match(line)
        if match and match.group(1) in consumed:
            continue
        if not stripped:
            continue
        for token in _TOKEN_RE.findall(stripped):
            if _is_identifier(token) and token not in found:
                found.append(token)
    return found


def _dedup(queries: list[str]) -> list[str]:
    """Убрать повторы, сохранив порядок: пункт может дословно повторить запрос."""
    seen: set[str] = set()
    out: list[str] = []
    for query in queries:
        norm = " ".join(query.split()).casefold()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(query)
    return out


def build_subqueries(task: dict | None, base_query: str) -> list[str]:
    """Набор подзапросов задачи; первый — продакшн-запрос целиком.

    base_query приходит аргументом, а не берётся из task_context: тот модуль
    импортирует этот, и обратный импорт замкнул бы цикл.

    Вырожденный вход (нет задачи, нет списков) даёт ровно [base_query] —
    поведение ретрива при этом тождественно однозапросному.
    """
    queries = [base_query]
    if not task:
        return _dedup(queries)
    text = "\n".join([
        str(task.get("title") or ""),
        str(task.get("description") or ""),
    ])
    items = _items(text)
    items.extend(str(c) for c in (task.get("criteria") or []) if str(c).strip())
    queries.extend(items)
    identifiers = _identifiers(text, items)
    if identifiers:
        queries.append(" ".join(identifiers[:MAX_IDENTIFIERS]))
    return _dedup(queries)[:MAX_SUBQUERIES]
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/mcp/test_subqueries.py -q`
Expected: PASS (8 тестов)

- [ ] **Step 5: Прогнать весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS, регрессий нет

- [ ] **Step 6: Коммит**

```bash
git add reviewer/mcp/subqueries.py tests/mcp/test_subqueries.py
git commit -m "feat(mcp): извлечение подзапросов из структуры и сущностей задачи

Пункт списка требований даёт свой подзапрос, идентификаторы прозаической
части — один пул. Число подзапросов производно от размера задачи, cap 20
против вырожденного текста. Чистый модуль: ни сети, ни БД, ни LLM."
```

---

### Task 2: RRF-слияние и обрезка блока

**Files:**
- Create: `reviewer/retrieval/multiquery.py`
- Test: `tests/retrieval/test_multiquery.py`

**Interfaces:**
- Consumes: `Retrieved` (`reviewer/index/store.py:35-46`, обычный mutable dataclass — `dataclasses.replace` применим); `ContextPack`, `_dedupe_overlapping`, `_is_test_path` из `reviewer/retrieval/retriever.py`.
- Produces: `RRF_K: int = 60`; `MAX_BLOCK_CHARS: int = 2000`; `rrf_merge(runs: list[list], k: int = RRF_K) -> list`; `cap_block(item, max_chars: int = MAX_BLOCK_CHARS)`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/retrieval/test_multiquery.py`:

```python
"""RRF-слияние выдач подзапросов и обрезка блока рендера (PRI-255)."""
from reviewer.index.store import Retrieved
from reviewer.retrieval.multiquery import MAX_BLOCK_CHARS, cap_block, rrf_merge


def _hit(node_id: str, start_line: int = 1, end_line: int = 2, text: str = "body"):
    path, fqn = node_id.split("#", 1)
    return Retrieved(node_id=node_id, path=path, symbol_fqn=fqn, kind="function",
                     start_line=start_line, end_line=end_line, text=text, score=1.0)


def test_found_by_two_subqueries_outranks_leader_of_one():
    # a.py#f второй в обоих прогонах, b.py#g первый в одном: сумма 1/62+1/62 > 1/61
    merged = rrf_merge([
        [_hit("b.py#g"), _hit("a.py#f")],
        [_hit("c.py#h"), _hit("a.py#f")],
    ])
    assert [it.node_id for it in merged][0] == "a.py#f"


def test_single_subquery_hit_still_present():
    merged = rrf_merge([[_hit("a.py#f")], [_hit("b.py#g")]])
    assert {it.node_id for it in merged} == {"a.py#f", "b.py#g"}


def test_score_is_rrf_sum():
    merged = rrf_merge([[_hit("a.py#f")], [_hit("a.py#f")]])
    assert merged[0].score == 2 * (1.0 / 61)


def test_ties_broken_deterministically_by_node_id():
    first = rrf_merge([[_hit("b.py#g")], [_hit("a.py#f")]])
    second = rrf_merge([[_hit("a.py#f")], [_hit("b.py#g")]])
    assert [it.node_id for it in first] == [it.node_id for it in second]


def test_empty_runs_yield_empty_result():
    assert rrf_merge([]) == []
    assert rrf_merge([[], []]) == []


def test_short_block_is_untouched():
    item = _hit("a.py#f", 10, 11, "one\ntwo")
    assert cap_block(item) is item


def test_long_block_is_cut_on_line_boundary_with_honest_end_line():
    body = "\n".join(f"строка {i}" * 20 for i in range(200))
    capped = cap_block(_hit("a.py#f", start_line=100, end_line=299, text=body))
    assert len(capped.text) <= MAX_BLOCK_CHARS
    assert capped.text.endswith(capped.text.splitlines()[-1]), "обрезка по границе строк"
    assert capped.end_line == 100 + len(capped.text.splitlines()) - 1
    assert capped.end_line < 299


def test_cap_block_does_not_mutate_source():
    item = _hit("a.py#f", 1, 400, "x" * 5000)
    cap_block(item)
    assert len(item.text) == 5000
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/retrieval/test_multiquery.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.retrieval.multiquery'`

- [ ] **Step 3: Написать минимальную реализацию**

Создать `reviewer/retrieval/multiquery.py` (оркестрация `search_multi` добавляется в Task 3, здесь только чистая часть):

```python
"""Мультизапросный ретрив с RRF-слиянием для секции code solve-task (PRI-255).

Путь параллелен Retriever.search_base и не меняет его: search_base общий для
/ask, грунтовки и ревью PR. Здесь зовётся то, что лежит ниже него —
store.hybrid_search, — а его хвост (дедуп, фильтр тестов) переиспользуется
импортом, а не копией.

Финальный ранкер — RRF, не реранкер. Причина измерена: cliff-отсечка считается
по скорам реранкера против того же многотемного запроса, на размытом запросе
все скоры низкие, отсечка падает до floor — отсюда медиана «2 файла» в
eval/replay_report.md. Сохранить реранк на исходном запросе значило бы
сохранить сам механизм потери.
"""
from __future__ import annotations

import dataclasses
import logging

log = logging.getLogger(__name__)

RRF_K = 60
"""Константа RRF — та же, что в store.hybrid_search и TaskStore.search."""

MAX_BLOCK_CHARS = 2000
"""Потолок символов на блок выдачи — четверть бюджета max_tool_result_chars.

Без него один чанк-класс на 400 строк выжигает весь символьный бюджет
as_context (text[:8000]), и остальные файлы до сборщика брифа не доезжают.
Файловые квоты и диверсификация — не здесь, это ID-310.
"""


def rrf_merge(runs: list[list], k: int = RRF_K) -> list:
    """Слить выдачи подзапросов: score(node) = Σ 1/(k + rank_в_прогоне).

    Файл, найденный несколькими подзапросами, поднимается наверх; найденный
    одним — всё равно остаётся в выдаче. Тай-брейк по node_id, поэтому
    порядок прогонов на результат не влияет.
    """
    scores: dict[str, float] = {}
    items: dict[str, object] = {}
    for run in runs:
        for rank, item in enumerate(run, start=1):
            scores[item.node_id] = scores.get(item.node_id, 0.0) + 1.0 / (k + rank)
            items.setdefault(item.node_id, item)
    ordered = sorted(items, key=lambda node_id: (-scores[node_id], node_id))
    return [dataclasses.replace(items[node_id], score=scores[node_id])
            for node_id in ordered]


def cap_block(item, max_chars: int = MAX_BLOCK_CHARS):
    """Обрезать текст блока по границе строк, поправив end_line.

    Границу строк держим не ради красоты: as_context нумерует строки от
    start_line, а extract_context_paths требует в заголовке диапазон
    ':\\d+-\\d+'. Обрезка по середине строки сделала бы заголовок ложью, а
    усечение уже в as_context — вовсе съело бы заголовок и потеряло путь.
    """
    if len(item.text) <= max_chars:
        return item
    kept: list[str] = []
    used = 0
    for line in item.text.split("\n"):
        if kept and used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    return dataclasses.replace(
        item, text="\n".join(kept),
        end_line=item.start_line + max(len(kept) - 1, 0))
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/retrieval/test_multiquery.py -q`
Expected: PASS (8 тестов)

- [ ] **Step 5: Коммит**

```bash
git add reviewer/retrieval/multiquery.py tests/retrieval/test_multiquery.py
git commit -m "feat(retrieval): RRF-слияние выдач подзапросов и обрезка блока

Формула Σ 1/(60+rank) — та же, что в store.hybrid_search; третьей копии в
проекте не появляется. Обрезка блока по границе строк с честным end_line:
без неё один большой чанк выжигает символьный бюджет as_context."
```

---

### Task 3: Батч-эмбеддинг запросов и оркестрация `search_multi`

**Files:**
- Modify: `reviewer/index/embeddings.py:162-172` (добавить метод после `embed_query`)
- Modify: `reviewer/retrieval/multiquery.py` (добавить `search_multi` и приватные хелперы)
- Test: `tests/index/test_embeddings.py` (дописать), `tests/retrieval/test_multiquery.py` (дописать)

**Interfaces:**
- Consumes: `rrf_merge`, `cap_block` (Task 2); `CodebaseLimits` (`reviewer/policy/context_limits.py:8-16`: `floor`, `ceiling`, `ratio`, `abs_floor`, `candidate_pool`, `ann_distance_max`); `base_ref` (`reviewer/index/refs.py`).
- Produces: `VoyageEmbedder.embed_queries(texts: list[str]) -> list[list[float]]`; `search_multi(retriever, repo: str, queries: list[str], *, limits=None, hops: int = 1, branch: str = "", include_tests: bool = False) -> ContextPack`.

- [ ] **Step 1: Написать падающий тест на батч-эмбеддинг**

Дописать в `tests/index/test_embeddings.py`:

```python
def test_embed_queries_batches_misses_and_reuses_cache():
    """Один сетевой вызов на все промахи; попадания кэша не эмбеддятся заново."""
    embedder = _embedder()          # существующий хелпер файла
    embedder.embed_query("первый")
    calls_before = len(embedder._client.calls)
    vectors = embedder.embed_queries(["первый", "второй", "третий", "второй"])
    assert len(vectors) == 4
    assert vectors[1] == vectors[3], "повтор в списке отдаёт тот же вектор"
    assert len(embedder._client.calls) == calls_before + 1, "промахи ушли одним батчем"


def test_embed_queries_uses_query_input_type():
    embedder = _embedder()
    embedder.embed_queries(["запрос"])
    assert embedder._client.calls[-1]["input_type"] == "query"
```

Если в файле нет хелпера `_embedder()` и фейкового клиента с полем `calls`, взять существующий фейк файла и адаптировать имена — тест должен опираться на то, что в файле уже есть, а не заводить второй фейк.

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `.venv/bin/pytest tests/index/test_embeddings.py -q`
Expected: FAIL — `AttributeError: 'VoyageEmbedder' object has no attribute 'embed_queries'`

- [ ] **Step 3: Реализовать `embed_queries`**

Добавить в `reviewer/index/embeddings.py` сразу после `embed_query`:

```python
    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Эмбеддинги нескольких запросов одним батчем через тот же LRU-кэш.

        Мультизапросный ретрив (PRI-255) считает N подзапросов, и N отдельных
        вызовов упёрлись бы в 3 RPM free tier. Блокировка на время сетевого
        вызова не держится: под ней только чтение и запись кэша.
        """
        if not texts:
            return []
        unique = list(dict.fromkeys(texts))
        with self._lock:
            cached = {}
            for text in unique:
                if text in self._query_cache:
                    self._query_cache.move_to_end(text)
                    cached[text] = self._query_cache[text]
        missing = [text for text in unique if text not in cached]
        fresh: dict[str, list[float]] = {}
        if missing:
            fresh = dict(zip(missing, self._embed(missing, "query")))
            with self._lock:
                for text, vec in fresh.items():
                    if len(self._query_cache) >= self._cache_size:
                        self._query_cache.popitem(last=False)
                    self._query_cache[text] = vec
        merged = {**cached, **fresh}
        return [merged[text] for text in texts]
```

- [ ] **Step 4: Запустить и убедиться, что проходит**

Run: `.venv/bin/pytest tests/index/test_embeddings.py -q`
Expected: PASS

- [ ] **Step 5: Написать падающий тест на `search_multi`**

Дописать в `tests/retrieval/test_multiquery.py`:

```python
from reviewer.policy.context_limits import CodebaseLimits
from reviewer.retrieval.multiquery import search_multi


class _FakeEmbedder:
    def __init__(self, fail_batch: bool = False):
        self.batches: list = []
        self.singles: list = []
        self._fail_batch = fail_batch

    def embed_queries(self, texts):
        if self._fail_batch:
            raise RuntimeError("нет квоты")
        self.batches.append(list(texts))
        return [[0.1] * 8 for _ in texts]

    def embed_query(self, text):
        self.singles.append(text)
        return [0.1] * 8


class _FakeStore:
    def __init__(self, by_query: dict, fail_for: str | None = None):
        self._by_query = by_query
        self._fail_for = fail_for
        self.queries: list = []

    def hybrid_search(self, repo, *, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k, candidates, base_ref="base"):
        self.queries.append(query_text)
        if query_text == self._fail_for:
            raise RuntimeError("сбой прогона")
        return list(self._by_query.get(query_text, []))

    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths, *, base_ref="base"):
        return []


class _Retriever:
    def __init__(self, store, embedder, graph=None, max_context_chars=8000):
        self.store, self.embedder, self.graph = store, embedder, graph
        self.max_context_chars = max_context_chars


def _bm25(node_id: str, text: str = "body"):
    item = _hit(node_id, text=text)
    item.bm25_hit = True
    return item


def test_one_batched_embedding_call_per_assembly():
    embedder = _FakeEmbedder()
    store = _FakeStore({"q0": [_bm25("a.py#f")], "q1": [_bm25("b.py#g")]})
    pack = search_multi(_Retriever(store, embedder), "o/n", ["q0", "q1"],
                        limits=CodebaseLimits(), branch="dev")
    assert embedder.batches == [["q0", "q1"]], "ровно один вызов эмбеддера"
    assert embedder.singles == []
    assert store.queries == ["q0", "q1"], "по прогону на подзапрос"
    assert {it.path for it in pack.items} == {"a.py", "b.py"}


def test_tail_subquery_only_hit_reaches_render():
    """Критерий приёмки 3: файл, найденный только хвостовым подзапросом."""
    store = _FakeStore({
        "q0": [_bm25("core.py#a")],
        "хвостовой пункт": [_bm25("tail.py#z")],
    })
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n",
                        ["q0", "хвостовой пункт"], limits=CodebaseLimits(), branch="dev")
    assert "tail.py" in pack.as_context(line_numbers=True)


def test_failed_batch_falls_back_to_single_query():
    embedder = _FakeEmbedder(fail_batch=True)
    store = _FakeStore({"q0": [_bm25("a.py#f")]})
    pack = search_multi(_Retriever(store, embedder), "o/n", ["q0", "q1"],
                        limits=CodebaseLimits(), branch="dev")
    assert embedder.singles == ["q0"]
    assert store.queries == ["q0"], "откат идёт по первому подзапросу"
    assert pack.items


def test_failed_run_is_skipped_and_others_merge():
    store = _FakeStore({"q0": [_bm25("a.py#f")], "q1": [_bm25("b.py#g")]}, fail_for="q0")
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0", "q1"],
                        limits=CodebaseLimits(), branch="dev")
    assert {it.path for it in pack.items} == {"b.py"}


def test_tests_are_filtered_unless_requested():
    store = _FakeStore({"q0": [_bm25("tests/test_a.py#t"), _bm25("a.py#f")]})
    retriever = _Retriever(store, _FakeEmbedder())
    without = search_multi(retriever, "o/n", ["q0"], limits=CodebaseLimits(), branch="dev")
    assert {it.path for it in without.items} == {"a.py"}
    with_tests = search_multi(retriever, "o/n", ["q0"], limits=CodebaseLimits(),
                              branch="dev", include_tests=True)
    assert "tests/test_a.py" in {it.path for it in with_tests.items}


def test_ann_prefilter_drops_distant_non_lexical_hit():
    far = _hit("far.py#x")
    far.bm25_hit, far.ann_distance = False, 0.99
    store = _FakeStore({"q0": [_bm25("a.py#f"), far]})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    assert {it.path for it in pack.items} == {"a.py"}


def test_ceiling_caps_merged_output():
    hits = [_bm25(f"f{i}.py#s") for i in range(40)]
    store = _FakeStore({"q0": hits})
    limits = CodebaseLimits(ceiling=5)
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=limits, branch="dev")
    assert len(pack.items) == 5


def test_blocks_are_capped_before_render():
    big = _bm25("a.py#f", text="\n".join("x" * 100 for _ in range(200)))
    store = _FakeStore({"q0": [big, _bm25("b.py#g")]})
    pack = search_multi(_Retriever(store, _FakeEmbedder()), "o/n", ["q0"],
                        limits=CodebaseLimits(), branch="dev")
    context = pack.as_context(line_numbers=True)
    assert "b.py" in context, "второй файл не вытеснен большим блоком"
    assert "[...truncated]" not in context
```

- [ ] **Step 6: Запустить и убедиться, что падает**

Run: `.venv/bin/pytest tests/retrieval/test_multiquery.py -q`
Expected: FAIL — `ImportError: cannot import name 'search_multi'`

- [ ] **Step 7: Реализовать `search_multi`**

Дописать в `reviewer/retrieval/multiquery.py` (импорты — вверх файла, рядом с существующими):

```python
from reviewer.index.refs import base_ref
from reviewer.retrieval.retriever import (
    ContextPack, _dedupe_overlapping, _is_test_path,
)
```

и функции в конец файла:

```python
def _embed_pairs(embedder, queries: list[str]) -> list[tuple]:
    """Пары (запрос, вектор) одним батчем; при сбое — только первый подзапрос.

    Откат идёт по первому подзапросу намеренно: он и есть продакшн-запрос
    целиком, поэтому деградация возвращает сегодняшнее поведение, а не пустоту.
    """
    if not queries:
        return []
    try:
        return list(zip(queries, embedder.embed_queries(queries)))
    except Exception:  # noqa: BLE001 — квота Voyage кончилась, это штатный случай
        log.warning("multiquery: батч-эмбеддинг недоступен — откат на один запрос",
                    exc_info=True)
        try:
            return [(queries[0], embedder.embed_query(queries[0]))]
        except Exception:  # noqa: BLE001
            log.warning("multiquery: эмбеддинг запроса недоступен", exc_info=True)
            return []


def _run(store, repo: str, query: str, qvec, lim, bref: str) -> list:
    """Один прогон гибрида с ANN-префильтром — тем же, что в search_base."""
    hits = store.hybrid_search(
        repo, query_text=query, query_embedding=qvec,
        overlay_ref="__none__", changed_paths=[],
        top_k=lim.candidate_pool, candidates=lim.candidate_pool, base_ref=bref)
    return [h for h in hits
            if getattr(h, "bm25_hit", False)
            or (getattr(h, "ann_distance", None) is not None
                and h.ann_distance <= lim.ann_distance_max)]


def _graph_items(retriever, repo: str, merged: list, ceiling: int, hops: int,
                 branch: str, bref: str, hybrid_ids: set) -> list:
    """Graph-expansion один раз, от топа слитого списка. Fail-soft."""
    if retriever.graph is None or not merged:
        return []
    try:
        seeds = [item.node_id for item in merged[:ceiling]]
        expanded = retriever.graph.expand_detailed(repo, seeds, hops=hops, branch=branch)
        graph_ids = [row["id"] for row in expanded]
        fetched = {item.node_id: item for item in retriever.store.fetch_nodes(
            repo, graph_ids, "__none__", [], base_ref=bref)}
        return [fetched[node_id] for node_id in graph_ids
                if node_id in fetched and node_id not in hybrid_ids]
    except Exception:  # noqa: BLE001
        log.warning("multiquery: graph-expansion недоступен", exc_info=True)
        return []


def search_multi(retriever, repo: str, queries: list[str], *, limits=None,
                 hops: int = 1, branch: str = "",
                 include_tests: bool = False) -> ContextPack:
    """Мультизапросный ретрив по base-индексу ветки: N прогонов, RRF, обрезка.

    Реранкера и cliff-отсечки здесь нет — финальный ранкер RRF (см. докстринг
    модуля). Порядок «сначала hybrid, потом graph-only» сохранён из search_base:
    hybrid приоритетен, граф добавляет разнообразие.
    """
    from reviewer.policy.context_limits import CodebaseLimits
    lim = limits or CodebaseLimits()
    bref = base_ref(branch)
    runs: list[list] = []
    for query, qvec in _embed_pairs(retriever.embedder, list(queries)):
        try:
            runs.append(_run(retriever.store, repo, query, qvec, lim, bref))
        except Exception:  # noqa: BLE001 — сбой одного прогона не роняет сборку
            log.warning("multiquery: прогон подзапроса не удался", exc_info=True)
    merged = rrf_merge(runs)
    hybrid_ids = {item.node_id for item in merged}
    items = [*merged, *_graph_items(retriever, repo, merged, lim.ceiling, hops,
                                    branch, bref, hybrid_ids)]
    if not include_tests:
        items = [item for item in items if not _is_test_path(item.path)]
    items = _dedupe_overlapping(items)[:lim.ceiling]
    return ContextPack(items=[cap_block(item) for item in items],
                       max_chars=retriever.max_context_chars)
```

- [ ] **Step 8: Запустить и убедиться, что проходит**

Run: `.venv/bin/pytest tests/retrieval/test_multiquery.py tests/index/test_embeddings.py -q`
Expected: PASS

- [ ] **Step 9: Убедиться, что `search_base` не тронут**

Run: `git diff --stat reviewer/retrieval/retriever.py`
Expected: пустой вывод — файл не менялся

- [ ] **Step 10: Прогнать весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 11: Коммит**

```bash
git add reviewer/index/embeddings.py reviewer/retrieval/multiquery.py \
        tests/index/test_embeddings.py tests/retrieval/test_multiquery.py
git commit -m "feat(retrieval): мультизапросный ретрив search_multi

N подзапросов эмбеддятся одним батчем (embed_queries поверх того же LRU),
N прогонов гибрида идут только в Postgres, выдачи сливаются RRF. Один
graph-expand от топа слитого списка. Реранка в этом пути нет: cliff по
скорам реранкера на многотемном запросе и есть механизм, режущий выдачу.

search_base не тронут — он общий для /ask, грунтовки и ревью PR."
```

---

### Task 4: Проводка в контекст задачи

**Files:**
- Modify: `reviewer/mcp/task_context.py:39-55` (добавить `_queries`/`_test_queries`), `:89-105` (передача списков)
- Modify: `reviewer/mcp/service.py` (новый `_search_codebase_multi`; `_TaskContextDeps.code` и `.test_exemplars`)
- Test: `tests/mcp/test_prepare_task_context.py` (дописать)

**Interfaces:**
- Consumes: `build_subqueries` (Task 1); `search_multi` (Task 3).
- Produces: `task_context._queries(task, key) -> list[str]`; `task_context._test_queries(task, key) -> list[str]`; `MCPReviewService._search_codebase_multi(repo, queries, branch=None, include_tests=False) -> str`. Протокол `deps`: `code(repo, branch, queries)` и `test_exemplars(repo, branch, queries)` теперь принимают **список**.

- [ ] **Step 1: Написать падающий тест**

В `tests/mcp/test_prepare_task_context.py` изменить сигнатуры фейка (`code` и `test_exemplars` принимают `queries`) и дописать:

```python
def test_code_section_receives_subquery_list():
    """Секция code ищется набором подзапросов, а не одной строкой."""
    seen = {}

    class Deps(FakeDeps):
        def code(self, repo, branch, queries):
            seen["code"] = queries
            return "reviewer/a.py#f (reviewer/a.py:1-3)"

        def test_exemplars(self, repo, branch, queries):
            seen["tests"] = queries
            return "tests/test_a.py#t"

    task_context.build_task_context(
        Deps(task={"key": "ID-1", "title": "T",
                   "description": "## Что сделать\n\n1. первый пункт\n2. второй пункт\n"}),
        repo="o/n", key="PRI-255", branch="dev", warm_board=False)

    assert isinstance(seen["code"], list)
    assert seen["code"][0] == task_context._query(
        {"key": "ID-1", "title": "T",
         "description": "## Что сделать\n\n1. первый пункт\n2. второй пункт\n"}, "PRI-255")
    assert any("второй пункт" in q for q in seen["code"])
    assert all(q.startswith("как тестируется: ") for q in seen["tests"])


def test_board_less_task_degenerates_to_single_query():
    """Без задачи в сторе набор равен одному запросу — поведение как прежде."""
    seen = {}

    class Deps(FakeDeps):
        def code(self, repo, branch, queries):
            seen["code"] = queries
            return ""

        def test_exemplars(self, repo, branch, queries):
            return ""

    task_context.build_task_context(Deps(task=None), repo="o/n",
                                    key="добавить эндпоинт логаута",
                                    branch="dev", warm_board=False)
    assert seen["code"] == ["добавить эндпоинт логаута"]


def test_test_queries_first_element_matches_single_test_query():
    task = {"title": "T", "description": "D"}
    assert task_context._test_queries(task, "PRI-1")[0] == task_context._test_query(task, "PRI-1")
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_prepare_task_context.py -q`
Expected: FAIL — `AttributeError: module 'reviewer.mcp.task_context' has no attribute '_test_queries'`

- [ ] **Step 3: Реализовать в `task_context.py`**

Добавить импорт вверху файла:

```python
from reviewer.mcp.subqueries import build_subqueries
```

Добавить после `_test_query` (сам `_test_query` оставить — он остаётся формой одиночного запроса и переиспользуется ниже):

```python
def _queries(task: dict | None, key: str) -> list[str]:
    """Набор подзапросов секции code: продакшн-запрос плюс структура задачи.

    Первый элемент — ровно _query(task, key), поэтому на задаче без списков
    набор вырождается в один запрос и ретрив ведёт себя как раньше.
    """
    return build_subqueries(task, _query(task, key))


def _test_queries(task: dict | None, key: str) -> list[str]:
    """Те же подзапросы, но про тесты области — префиксом, как _test_query."""
    return [f"как тестируется: {query}" for query in _queries(task, key)]
```

В `build_task_context` заменить два вызова (строки 99-105), оставив `query` для секций `related.similar` и `subsystems` без изменений:

```python
    payload["code"] = _safe(
        payload, "code", lambda: deps.code(repo, branch, _queries(task, key)), "",
        "поиск по коду недоступен")
    payload["test_exemplars"] = _safe(
        payload, "test_exemplars",
        lambda: deps.test_exemplars(repo, branch, _test_queries(task, key)), "",
        "поиск по тестам недоступен")
```

- [ ] **Step 4: Реализовать в `service.py`**

Добавить метод рядом с `search_codebase` (сам `search_codebase` **не менять**):

```python
    def _search_codebase_multi(self, repo: str, queries: list[str],
                               branch: str | None = None,
                               include_tests: bool = False) -> str:
        """Мультизапросный ретрив секций контекста задачи (PRI-255).

        Приватный: публичный search_codebase остаётся однозапросным, чтобы
        /ask, грунтовка и ревью PR не меняли поведение.
        """
        from reviewer.retrieval.multiquery import search_multi
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        cl = self._resolve_context_limits(repo, resolved)
        try:
            pack = search_multi(
                self.components.retriever, repo, queries,
                limits=cl.search_codebase, hops=cl.graph.hops,
                branch=resolved, include_tests=include_tests)
        except Exception:
            log.warning("_search_codebase_multi: сбой поиска", exc_info=True)
            return "(ничего не найдено)"
        return pack.as_context(line_numbers=True) or "(ничего не найдено)"
```

И заменить два метода `_TaskContextDeps`:

```python
    def code(self, repo: str, branch: str, queries: list) -> str:
        return self._service._search_codebase_multi(repo, queries, branch, False)

    def test_exemplars(self, repo: str, branch: str, queries: list) -> str:
        return self._service._search_codebase_multi(repo, queries, branch, True)
```

- [ ] **Step 5: Запустить и убедиться, что проходит**

Run: `.venv/bin/pytest tests/mcp -q`
Expected: PASS

- [ ] **Step 6: Прогнать весь unit-набор и линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer/mcp/subqueries.py reviewer/retrieval/multiquery.py reviewer/mcp/task_context.py`
Expected: PASS, ruff чист по этим файлам

- [ ] **Step 7: Коммит**

```bash
git add reviewer/mcp/task_context.py reviewer/mcp/service.py tests/mcp/test_prepare_task_context.py
git commit -m "feat(mcp): секции code и test_exemplars ищутся набором подзапросов

deps.code и deps.test_exemplars принимают список запросов; первый элемент —
прежний продакшн-запрос, поэтому board-less вход и задача без списков ведут
себя как раньше. Публичный тул search_codebase не тронут."
```

---

### Task 5: Замер — вариант replay и распределение подзапросов

**Files:**
- Modify: `eval/solve_task_metrics/live.py` (метод `code_multi` после `code`)
- Modify: `eval/solve_task_metrics/variants.py:36-75` (вариант `multiquery` в `_REGISTRY`)
- Create: `eval/solve_task_metrics/subquery_stats.py`
- Modify: `eval/solve_task_metrics/__main__.py` (подкоманда `subqueries`)
- Test: `tests/eval/test_variants.py` (дописать), `tests/eval/test_subquery_stats.py` (создать)

**Interfaces:**
- Consumes: `build_subqueries` (Task 1); `search_multi` (Task 3); `_search_codebase_multi` (Task 4); `extract_context_paths` (`eval/solve_task_metrics/context_paths.py:18-25`); `TaskInput(key, task, query)` и `ReplayTarget(repo, branch, limits)` (`variants.py:27-42`).
- Produces: `LiveRetrieval.code_multi(repo, branch, queries, limits) -> str`; вариант `"multiquery"` в `variants.VARIANT_NAMES`; `subquery_stats.size_bucket(task) -> str`, `subquery_stats.distribution(rows) -> list[dict]`, `subquery_stats.render(rows) -> str`.

**Почему распределение — отдельная подкоманда, а не поле снимка:** контракт стратегии варианта возвращает `set` путей. Протаскивание метаданных сломало бы его для всех вариантов и потребовало бы бампа `SCHEMA` в `replay.py:18`.

- [ ] **Step 1: Написать падающий тест варианта**

Дописать в `tests/eval/test_variants.py`:

```python
def test_multiquery_variant_passes_subquery_list():
    """Вариант зовёт code_multi продакшн-набором подзапросов, а не одной строкой."""
    class MultiProvider(FakeProvider):
        def code_multi(self, repo, branch, queries, limits):
            self.calls.append((repo, branch, list(queries), limits))
            return HEADER

    provider = MultiProvider(HEADER)
    task = variants.TaskInput(
        key="PRI-1",
        task={"title": "T", "description": "## Что сделать\n\n1. первый\n2. хвостовой\n"},
        query="q")
    target = variants.ReplayTarget(repo="o/n", branch="dev", limits=None)

    assert variants.get_variant("multiquery")(provider, task, target) == {"reviewer/a.py"}
    _repo, _branch, queries, limits = provider.calls[0]
    assert queries[0] == "q", "продакшн-запрос идёт первым"
    assert any("хвостовой" in q for q in queries)
    assert limits is None


def test_multiquery_is_registered():
    assert "multiquery" in variants.VARIANT_NAMES
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `.venv/bin/pytest tests/eval/test_variants.py -q`
Expected: FAIL — `UnknownVariant: неизвестный вариант 'multiquery'`

- [ ] **Step 3: Реализовать вариант**

В `eval/solve_task_metrics/variants.py` добавить импорт и стратегию:

```python
from reviewer.mcp.subqueries import build_subqueries
```

```python
def _multiquery(provider, task: TaskInput, target: ReplayTarget) -> set:
    """Мультизапрос по структуре и сущностям задачи с RRF-слиянием (PRI-255).

    Набор подзапросов строится ПРОДАКШН-функцией: своей копии формулы здесь
    не заводится, иначе replay мерил бы не тот вход, что видит прод.
    """
    queries = build_subqueries(task.task, task.query)
    text = provider.code_multi(target.repo, target.branch, queries, target.limits)
    return extract_context_paths(text)
```

и строку реестра:

```python
_REGISTRY = {
    "baseline": _baseline,
    "limits": _limits,
    "multiquery": _multiquery,
}
```

- [ ] **Step 4: Реализовать `code_multi` в живом провайдере**

В `eval/solve_task_metrics/live.py` добавить после `code`:

```python
    def code_multi(self, repo: str, branch: str, queries: list, limits: dict | None) -> str:
        """Мультизапросная выдача тем же продакшн-путём, что видит сборщик брифа."""
        if not limits:
            return self._service._search_codebase_multi(repo, list(queries), branch, False)
        from reviewer.retrieval.multiquery import search_multi
        base = limits_to_yaml(self._service._resolve_context_limits(repo, branch))
        effective = ContextLimits.from_review_yaml(
            {"context_limits": _merge(base, limits)}
        )
        pack = search_multi(
            self._components.retriever, repo, list(queries),
            limits=effective.search_codebase, hops=effective.graph.hops,
            branch=branch, include_tests=False,
        )
        return pack.as_context(line_numbers=True) or "(ничего не найдено)"
```

- [ ] **Step 5: Написать падающий тест распределения**

Создать `tests/eval/test_subquery_stats.py`:

```python
"""Распределение числа подзапросов по размеру задачи (PRI-255, критерий 1)."""
from eval.solve_task_metrics import subquery_stats


def _task(lines: int, items: int = 0) -> dict:
    body = "\n".join(f"строка {i}" for i in range(lines))
    todo = "\n".join(f"{i}. пункт {i}" for i in range(1, items + 1))
    description = body + (f"\n\n## Что сделать\n\n{todo}\n" if items else "")
    return {"title": "T", "description": description}


def test_size_buckets_split_small_medium_bulk():
    assert "мелкая" in subquery_stats.size_bucket(_task(3))
    assert "средняя" in subquery_stats.size_bucket(_task(20))
    assert "развёртка" in subquery_stats.size_bucket(_task(50))


def test_distribution_is_not_a_constant_across_buckets():
    rows = subquery_stats.distribution([
        ("PRI-1", _task(3), "q"),
        ("PRI-2", _task(50, items=10), "q"),
    ])
    counts = {row["bucket"]: row["median"] for row in rows}
    assert len(set(counts.values())) > 1, "число подзапросов производно от размера задачи"


def test_render_lists_every_bucket_and_task_count():
    text = subquery_stats.render([("PRI-1", _task(3), "q"), ("PRI-2", _task(50, 10), "q")])
    assert "мелкая" in text and "развёртка" in text
    assert "| задач |" in text or "задач" in text


def test_missing_task_counts_as_single_subquery():
    rows = subquery_stats.distribution([("PRI-9", None, "формулировка")])
    assert rows[0]["median"] == 1
```

- [ ] **Step 6: Запустить и убедиться, что падает**

Run: `.venv/bin/pytest tests/eval/test_subquery_stats.py -q`
Expected: FAIL — `ImportError: cannot import name 'subquery_stats'`

- [ ] **Step 7: Реализовать `subquery_stats.py`**

Создать `eval/solve_task_metrics/subquery_stats.py`:

```python
"""Распределение числа подзапросов по размеру задачи (PRI-255, критерий 1).

Критерий приёмки требует показать, что число подзапросов производно от
размера задачи, а не константа. Расчёт детерминированный и не трогает
ретрив: нужен только текст задачи, поэтому подкоманда дешёвая и не тратит
квоту Voyage.

Формула набора подзапросов — продакшн-функция build_subqueries; своей копии
здесь нет.
"""
from __future__ import annotations

import statistics

from reviewer.mcp.subqueries import build_subqueries

SMALL_MAX_LINES = 10
MEDIUM_MAX_LINES = 30

BUCKETS = (
    f"мелкая (≤{SMALL_MAX_LINES} строк)",
    f"средняя ({SMALL_MAX_LINES + 1}-{MEDIUM_MAX_LINES})",
    f"развёртка (>{MEDIUM_MAX_LINES})",
)


def size_bucket(task: dict | None) -> str:
    """Класс размера задачи по числу строк описания."""
    lines = len(str((task or {}).get("description") or "").splitlines())
    if lines <= SMALL_MAX_LINES:
        return BUCKETS[0]
    if lines <= MEDIUM_MAX_LINES:
        return BUCKETS[1]
    return BUCKETS[2]


def distribution(rows) -> list[dict]:
    """Сводка по классам размера: число задач, медиана/мин/макс подзапросов.

    rows — последовательность (key, task, base_query).
    """
    by_bucket: dict[str, list[int]] = {}
    for _key, task, base_query in rows:
        count = len(build_subqueries(task, base_query))
        by_bucket.setdefault(size_bucket(task), []).append(count)
    return [
        {
            "bucket": bucket,
            "tasks": len(counts),
            "median": statistics.median(counts),
            "min": min(counts),
            "max": max(counts),
        }
        for bucket in BUCKETS
        if (counts := by_bucket.get(bucket))
    ]


def render(rows) -> str:
    """Markdown-таблица распределения — то, что уходит в отчёт приёмки."""
    lines = [
        "| класс задачи | задач | медиана подзапросов | мин | макс |",
        "|---|---|---|---|---|",
    ]
    for row in distribution(rows):
        lines.append(
            f"| {row['bucket']} | {row['tasks']} | {row['median']} "
            f"| {row['min']} | {row['max']} |"
        )
    return "\n".join(lines)
```

- [ ] **Step 8: Добавить подкоманду `subqueries`**

В `eval/solve_task_metrics/__main__.py` добавить в блок импортов `subquery_stats`, функцию команды рядом с `cmd_replay`:

```python
def cmd_subqueries(args) -> int:
    """Распределение числа подзапросов по корпусу (PRI-255, критерий 1)."""
    provider, repo, branch = live.open_live(args.repo, args.branch)
    with provider:
        rows = []
        for key in replay_mod.corpus_keys(BRIEFS_DIR):
            task = provider.task(key)
            rows.append((key, task, provider.query(task, key)))
    print(f"Корпус: {len(rows)} задач, репозиторий {repo}@{branch}")
    print(subquery_stats.render(rows))
    return 0
```

регистрацию парсера рядом с `replay_parser`:

```python
    subqueries_parser = subparsers.add_parser(
        "subqueries",
        help="распределение числа подзапросов ретрива по размеру задачи",
    )
    subqueries_parser.add_argument(
        "--repo", default=None, help="owner/name; по умолчанию DEFAULT_REPO"
    )
    subqueries_parser.add_argument(
        "--branch", default=None, help="ветка; по умолчанию первичная"
    )
```

и ветку в `main`:

```python
    if args.command == "subqueries":
        return cmd_subqueries(args)
```

Имена `live`, `replay_mod`, `BRIEFS_DIR` взять ровно те, что уже используются в файле (см. блок импортов `:17-32` и `cmd_replay`); если константа каталога брифов названа иначе — использовать существующее имя, а не заводить второе.

- [ ] **Step 9: Запустить тесты**

Run: `.venv/bin/pytest tests/eval -q`
Expected: PASS

- [ ] **Step 10: Проверить, что CLI собирается и подкоманда видна**

Run: `.venv/bin/python -m eval.solve_task_metrics --help`
Expected: в списке команд есть `subqueries` и `replay`; вывод не падает

- [ ] **Step 11: Прогнать весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 12: Коммит**

```bash
git add eval/solve_task_metrics/live.py eval/solve_task_metrics/variants.py \
        eval/solve_task_metrics/subquery_stats.py eval/solve_task_metrics/__main__.py \
        tests/eval/test_variants.py tests/eval/test_subquery_stats.py
git commit -m "feat(eval): вариант replay multiquery и распределение подзапросов

Вариант зовёт продакшн-путь через code_multi и строит набор подзапросов
продакшн-функцией build_subqueries — своей копии формулы в харнессе нет.
Распределение числа подзапросов по размеру задачи — отдельная дешёвая
подкоманда: контракт варианта возвращает множество путей, и протаскивание
метаданных сломало бы его для всех вариантов."
```

---

### Task 6: Приёмочный прогон и документация

**Дорогой шаг.** Требует поднятой инфраструктуры, свежего base-индекса и живой квоты Voyage (3 RPM). Корпус — 55 задач. Выполнять отдельно от юнит-работы.

**Files:**
- Modify: `eval/replay_report.md` (перезаписывается командой), `eval/replay_history.jsonl` (дописывается)
- Modify: `README.md`, `README.ru.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: всё из задач 1-5.
- Produces: зафиксированная величина дельты bulk core-recall; распределение подзапросов; документированные инварианты.

- [ ] **Step 1: Поднять инфраструктуру и проверить свежесть индекса**

```bash
docker compose up -d
uvx --from rag-reviewer reviewer status . --branch dev --json
```
Expected: `drift == 0`. Если нет — `uvx --from rag-reviewer reviewer index . --ref dev --repo mimfort/rag_for_git`.

- [ ] **Step 2: Снять распределение подзапросов (критерий 1)**

Run: `.venv/bin/python -m eval.solve_task_metrics subqueries --repo mimfort/rag_for_git --branch dev`
Expected: таблица, в которой медиана подзапросов **различается** между классами размера (мелкая / средняя / развёртка). Сохранить вывод — он идёт в отчёт.

- [ ] **Step 3: Прогнать A/B против baseline (критерий 2)**

Run: `.venv/bin/python -m eval.solve_task_metrics replay --variant multiquery --repo mimfort/rag_for_git --branch dev`
Expected: команда сама снимет baseline, если его нет в истории; в конце печатает агрегат. Прогон долгий (Voyage 3 RPM) — не прерывать.

- [ ] **Step 4: Проверить критерий 2 по отчёту**

Run: `head -30 eval/replay_report.md`
Expected: `core-recall bulk` варианта `multiquery` **выше** baseline (`0.127`), обе стороны на одном `indexed_sha`, ни одна не помечена частичной. Если дельта неположительная — не «подкручивать» константы наугад: занести фактические числа и разбор в отчёт, доложить пользователю и остановиться. Отрицательный результат — тоже результат, и он должен быть виден.

- [ ] **Step 5: Показать хвостовой пункт на задаче-развёртке (критерий 3)**

Взять из таблицы «Дельта по задачам» задачу с наибольшим `приобретено`, класса «развёртка», и показать, какие файлы пришли только от хвостового подзапроса:

```bash
.venv/bin/python - <<'PY'
from eval.solve_task_metrics import live, variants
from reviewer.mcp.subqueries import build_subqueries
from eval.solve_task_metrics.context_paths import extract_context_paths

KEY = "PRI-217"   # подставить фактический ключ задачи-развёртки из отчёта
provider, repo, branch = live.open_live("mimfort/rag_for_git", "dev")
with provider:
    task = provider.task(KEY)
    base = provider.query(task, KEY)
    queries = build_subqueries(task, base)
    print(f"{KEY}: подзапросов {len(queries)}")
    head = extract_context_paths(provider.code_multi(repo, branch, queries[:1], None))
    full = extract_context_paths(provider.code_multi(repo, branch, queries, None))
    print("только от хвостовых подзапросов:", sorted(full - head))
PY
```
Expected: непустое множество файлов, найденных только хвостовыми подзапросами. Сохранить вывод для отчёта.

- [ ] **Step 6: Дописать разбор в отчёт**

Дописать в конец `eval/replay_report.md` секцию `## Приёмка PRI-255` с тремя блоками: таблица распределения подзапросов (шаг 2), фактическая дельта bulk core-recall с обеими цифрами (шаг 4), список файлов от хвостовых подзапросов на названной задаче (шаг 5).

- [ ] **Step 7: Обновить документацию**

- `README.md` и `README.ru.md`: в описании ретрива solve-task указать, что секция `code` ищется набором подзапросов с RRF-слиянием, а публичный `search_codebase` остаётся однозапросным. Править **синхронно оба файла**.
- `CLAUDE.md`, блок «Неочевидные факты» — добавить пункт: секция `code` контекста задачи идёт мультизапросом с RRF-слиянием, финальный ранкер — RRF, а не реранкер (cliff по скорам реранкера на многотемном запросе и был механизмом, режущим выдачу до медианы 2 файлов); блок выдачи обрезается до `MAX_BLOCK_CHARS`, потому что `as_context` режет рендер тупым `text[:8000]` и один большой чанк выжигал весь бюджет; `search_base` и публичный `search_codebase` не тронуты.

- [ ] **Step 8: Прогнать весь unit-набор и линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer/ eval/`
Expected: тесты зелёные; ruff по изменённым файлам чист (repo-wide чистоты не ждать — на dev она не достигнута).

- [ ] **Step 9: Коммит**

```bash
git add eval/replay_report.md eval/replay_history.jsonl README.md README.ru.md CLAUDE.md
git commit -m "docs(eval): приёмка PRI-255 — распределение подзапросов и дельта replay

Зафиксированы: распределение числа подзапросов по классам размера задачи,
дельта bulk core-recall multiquery против baseline на одном indexed_sha и
файлы, найденные только хвостовыми подзапросами задачи-развёртки."
```

- [ ] **Step 10: Доложить пользователю и запросить подтверждение на push и PR**

`git push` и создание PR требуют явного подтверждения пользователя — не выполнять их самостоятельно.

---

## Self-Review

**1. Покрытие спеки.**

| Требование спеки | Задача |
|---|---|
| `build_subqueries`: пункты + идентификаторы, cap 20 | Task 1 |
| Число подзапросов производно от размера | Task 1 (реализация), Task 5 + 6 (замер, критерий 1) |
| `rrf_merge` формулой `Σ 1/(60+rank)` | Task 2 |
| Предохранитель рендера `MAX_BLOCK_CHARS = 2000` | Task 2 |
| Один вызов Voyage: `embed_queries` | Task 3 |
| `search_multi`: N прогонов, один graph-expand, потолок, без реранка | Task 3 |
| Fail-open по каждому отказу | Task 3 (тесты на откат батча и падение прогона) |
| `search_base` и публичный `search_codebase` не тронуты | Task 3 Step 9 (проверка diff), Task 4 (приватный метод) |
| Проводка `code` + `test_exemplars` | Task 4 |
| Вырожденный вход тождественен прежнему поведению | Task 1, Task 4 (тест board-less) |
| Вариант replay `multiquery` | Task 5 |
| Подкоманда `subqueries` | Task 5 |
| Критерий 2: дельта против replay-baseline 0.127 | Task 6 |
| Критерий 3: хвостовой пункт находит свои файлы | Task 3 (юнит), Task 6 Step 5 (на корпусе) |
| Критерий 4: ноль LLM, один вызов эмбеддера | Task 3 (тест числа вызовов) |
| Границы скоупа: ID-310/311/312 не затрагиваются | ни одна задача не меняет cliff, файловые квоты и `subsystems` |

Пробелов нет.

**2. Плейсхолдеры.** Не найдено: в каждом шаге с кодом приведён рабочий код, в каждом шаге с прогоном — точная команда и ожидаемый результат. Два места сознательно требуют подстановки из окружения: имя фейкового клиента в `tests/index/test_embeddings.py` (Task 3 Step 1) и ключ задачи-развёртки из отчёта (Task 6 Step 5) — оба сопровождены указанием, откуда взять значение.

**3. Согласованность типов.** `build_subqueries(task, base_query) -> list[str]` — одна сигнатура в задачах 1, 4, 5. `search_multi(retriever, repo, queries, *, limits, hops, branch, include_tests) -> ContextPack` — одна в задачах 3, 5. `deps.code(repo, branch, queries)` со списком — согласованно в задачах 4 и в фейке теста. `code_multi(repo, branch, queries, limits)` — одна форма в `live.py` и в фейке `tests/eval/test_variants.py`. Константы `MAX_SUBQUERIES`/`MAX_BLOCK_CHARS`/`RRF_K` объявлены по одному разу и совпадают с Global Constraints.

---

## Разрешение стратегии `auto`

Затронутых файлов: 9 продакшн/харнесс + 6 тестовых = **15** (> 10). Правило 1 рубрики срабатывает первым → стратегия **`subagent`** (`superpowers:subagent-driven-development`). Риск-сигналов при этом нет: миграций схемы нет, контракт публичного MCP-тула не меняется, кредов задача не касается, необратимых внешних действий в плане нет (push и PR вынесены под подтверждение пользователя).
