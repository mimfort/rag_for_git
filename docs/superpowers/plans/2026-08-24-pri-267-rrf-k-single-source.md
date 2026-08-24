# PRI-267 — единственный источник константы RRF k: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать числовой литерал `60` из обоих SQL так, чтобы константа RRF имела ровно одно объявление и расхождение стало невозможным, а не обнаруживаемым.

**Architecture:** Новый модуль `reviewer/rrf.py` в корне пакета держит `RRF_K = 60`. Оба store (`reviewer/index/store.py`, `reviewer/tasks/store.py`) получают значение именованным параметром `%(rrf_k)s::int` вместо литерала; `reviewer/retrieval/multiquery.py` импортирует константу вместо собственного объявления. Guard-тест проверяет фактически переданные драйверу SQL и `params`.

**Tech Stack:** Python 3, psycopg3 + psycopg_pool, pgvector, ParadeDB (pg_search/BM25), pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-pri-267-rrf-k-single-source-design.md`

## Global Constraints

- Ветка `feat/pri-267-rrf-k-single-source` уже создана и содержит коммит `140dc6e` с брифом и спекой. Работать в ней; не переключаться и не создавать новых веток.
- Язык проекта — русский: комментарии, докстринги, сообщения. Новый код пишется по-русски.
- Коммиты — Conventional Commits на русском (`feat(index): …`, `test(retrieval): …`, `docs: …`). **Без self-attribution:** никаких `Co-Authored-By`, никаких упоминаний Claude.
- Unit-тестам запрещены внешняя сеть и localhost-сокеты. Guard-тест обязан работать без Postgres — соединение мокается.
- Integration-тесты обязаны нести `@pytest.mark.integration`; обычный `pytest` их исключает (`addopts = -m 'not integration'`).
- Правка любого контента под `plugin/` меняет codex payload-digest → в том же коммите обязателен прогон `.venv/bin/python scripts/update_codex_plugin_manifest.py`, иначе install-тесты краснеют.
- Никогда не выполнять `docker compose --profile test down -v` — это снесёт контейнеры и тома разработки. Безопасно только `docker compose --profile test rm -sfv paradedb-test neo4j-test`.
- Значение константы не меняется: было и остаётся `60`. Любое расхождение выдачи до/после — регрессия, а не ожидаемый эффект.

---

## File Structure

| Файл | Ответственность |
|---|---|
| `reviewer/rrf.py` | **новый.** Единственное объявление `RRF_K`. Ничего не импортирует — поэтому его может импортировать любой слой без цикла. |
| `reviewer/index/store.py` | `ChunkStore.hybrid_search`: два литерала в CTE `rrf` → параметр; ключ `rrf_k` в `params`. |
| `reviewer/tasks/store.py` | `TaskStore.search`: то же. |
| `reviewer/retrieval/multiquery.py` | Объявление константы удаляется, значение импортируется; `rrf_merge` не меняется. |
| `tests/test_rrf_k_single_source.py` | **новый.** Guard: значение доезжает до обоих SQL параметром, литерала нет, второго объявления нет. Лежит в корне `tests/`, потому что покрывает оба store и ни одной подсистеме не принадлежит (прецедент — `tests/test_ci_gates.py`). |
| `CLAUDE.md` | Абзац про долг переписывается: долг закрыт. |
| `plugin/skills/solve-task/references/brief-format.md` | Числовая копия `k` в промпте убирается вовсе. |

---

### Task 1: Единственный источник константы и параметризация обоих SQL

Один гейт рецензента: «расхождение k стало невозможным». Объявление, обе правки SQL и guard едут вместе — по отдельности каждая половина бессмысленна (константа без потребителей; параметр без теста, доказывающего, что он доезжает).

**Files:**
- Create: `reviewer/rrf.py`
- Create: `tests/test_rrf_k_single_source.py`
- Modify: `reviewer/index/store.py:475-512` (метод `hybrid_search`)
- Modify: `reviewer/tasks/store.py:253-285` (метод `TaskStore.search`)
- Modify: `reviewer/retrieval/multiquery.py:19-29` (импорты и объявление константы)

**Interfaces:**
- Consumes: ничего от предыдущих задач — это первая.
- Produces: модуль `reviewer.rrf` с единственным именем `RRF_K: int` (значение `60`). Задача 3 ссылается на путь `reviewer/rrf.py` в документации; задача 2 импортирует `RRF_K` для сборки SQL замера.

- [ ] **Step 1: Написать падающий guard-тест**

Создать `tests/test_rrf_k_single_source.py` целиком:

```python
"""Guard: константа RRF объявлена ровно один раз (PRI-267).

Значение k доезжает до обоих SQL именованным параметром из reviewer/rrf.py.
Тест смотрит на ФАКТИЧЕСКИ переданные драйверу sql и params, а не на текст
исходника: проверка по подстроке ловила бы форматирование, а не значение
(тот же урок, что в докстринге tests/metrics/test_reexport_guard.py).

Соединение мокается — тесту не нужны ни Postgres, ни сокет.
"""
from __future__ import annotations

import inspect
import re
from unittest.mock import patch

from reviewer.index.store import ChunkStore
from reviewer.retrieval import multiquery
from reviewer.rrf import RRF_K
from reviewer.tasks.store import TaskStore

# Числовой литерал в знаменателе RRF — ровно то, что задача убрала.
# После правки перед "+ rank" стоит каст "::int", а не цифра.
_LITERAL = re.compile(r"\d+\s*\+\s*rank")
_PLACEHOLDER = "%(rrf_k)s"


class _Cursor:
    """Курсор-заглушка: запросу нечего вернуть, важен сам факт вызова."""

    @staticmethod
    def fetchall() -> list:
        return []


class _Connection:
    """Соединение-заглушка, запоминающее переданные sql и params."""

    def __init__(self, captured: list[tuple[str, dict]]) -> None:
        self._captured = captured

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql: str, params: dict) -> _Cursor:
        self._captured.append((sql, params))
        return _Cursor()


def _capture_chunk_search() -> tuple[str, dict]:
    """Вызвать ChunkStore.hybrid_search и перехватить запрос."""
    captured: list[tuple[str, dict]] = []
    store = ChunkStore("postgresql://unused")     # пул ленивый, соединения не будет
    with patch.object(store, "_connect", return_value=_Connection(captured)):
        store.hybrid_search("owner/name", "запрос", [0.0] * 8, "pr:1", [],
                            base_ref="base:dev")
    assert len(captured) == 1
    return captured[0]


def _capture_task_search() -> tuple[str, dict]:
    """Вызвать TaskStore.search и перехватить запрос."""
    captured: list[tuple[str, dict]] = []
    store = TaskStore("postgresql://unused")
    with patch.object(store, "_connect", return_value=_Connection(captured)):
        store.search("запрос", [0.0] * 8)
    assert len(captured) == 1
    return captured[0]


def test_chunk_store_passes_rrf_k_as_parameter():
    """hybrid_search берёт k параметром из reviewer.rrf, а не литералом."""
    sql, params = _capture_chunk_search()
    assert params["rrf_k"] == RRF_K
    # Ровно две ветки CTE (bm25 и ann): одна подставленная и одна забытая
    # разъехались бы молча — это и есть чинимый дефект.
    assert sql.count(_PLACEHOLDER) == 2
    assert not _LITERAL.search(sql)


def test_task_store_passes_rrf_k_as_parameter():
    """TaskStore.search берёт k параметром из reviewer.rrf, а не литералом."""
    sql, params = _capture_task_search()
    assert params["rrf_k"] == RRF_K
    assert sql.count(_PLACEHOLDER) == 2
    assert not _LITERAL.search(sql)


def test_multiquery_does_not_redeclare_rrf_k():
    """Второго объявления нет: multiquery импортирует значение, а не задаёт своё."""
    source = inspect.getsource(multiquery)
    assert not re.search(r"^RRF_K\s*=\s*\d", source, re.M)
    assert multiquery.RRF_K == RRF_K
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/test_rrf_k_single_source.py -v`

Expected: FAIL на **сборке файла** — `ModuleNotFoundError: No module named 'reviewer.rrf'`: модуля ещё нет, а тест импортирует его на верхнем уровне, поэтому до отдельных тестов дело не доходит. Это ожидаемое первое падение, а не ошибка в тесте.

Чтобы увидеть содержательный red по каждому тесту, повторить запуск после Шага 3 (модуль создан, SQL ещё не тронут) — тогда ожидается: `test_chunk_store_passes_rrf_k_as_parameter` и `test_task_store_passes_rrf_k_as_parameter` падают на `KeyError: 'rrf_k'`, `test_multiquery_does_not_redeclare_rrf_k` — на `AssertionError`, потому что `multiquery.py:26` объявляет константу сам.

- [ ] **Step 3: Создать модуль-источник**

Создать `reviewer/rrf.py`:

```python
"""Константа RRF — единственная в системе (PRI-267).

Живёт в корне пакета, а не в retrieval рядом с ``rrf_merge``: её читают оба
store (``reviewer/index/store.py``, ``reviewer/tasks/store.py``), которые лежат
НИЖЕ retrieval. Импорт retrieval→index развернул бы направление зависимости —
``reviewer/retrieval/multiquery.py`` уже импортирует ``reviewer.index.refs``.

Модуль намеренно ничего не импортирует: тогда его может взять любой слой, не
рискуя циклом.
"""
from __future__ import annotations

RRF_K = 60
"""Знаменатель RRF: score = Σ 1/(RRF_K + rank). Одно объявление на систему —
питоновское слияние подзапросов и оба SQL берут значение отсюда."""
```

- [ ] **Step 4: Параметризовать SQL в `reviewer/index/store.py`**

Добавить импорт в шапку файла, рядом с существующими импортами `reviewer` (их там сейчас нет — поставить после блока сторонних импортов, перед `_BM25_STRIP`):

```python
from reviewer.rrf import RRF_K
```

В `hybrid_search` заменить блок CTE `rrf` (строки 493-497):

```python
        rrf AS (
            SELECT id, 1.0/(60+rank) AS s FROM bm25
            UNION ALL
            SELECT id, 1.0/(60+rank) AS s FROM ann
        )
```

на:

```python
        rrf AS (
            SELECT id, 1.0/(%(rrf_k)s::int + rank) AS s FROM bm25
            UNION ALL
            SELECT id, 1.0/(%(rrf_k)s::int + rank) AS s FROM ann
        )
```

и добавить ключ в `params` (строки 508-510):

```python
        params = {"repo": repo, "q": _bm25_query(query_text), "vec": Vector(query_embedding),
                  "overlay": overlay_ref, "changed": changed_paths,
                  "cand": candidates, "k": top_k, "base": base_ref, "rrf_k": RRF_K}
```

Каст `::int` обязателен: без него тип параметра выводится из контекста сложения и может разойтись между simple- и prepared-протоколом psycopg.

- [ ] **Step 5: Параметризовать SQL в `reviewer/tasks/store.py`**

Добавить импорт в шапку файла (после блока сторонних импортов, перед `_BM25_STRIP`):

```python
from reviewer.rrf import RRF_K
```

В `TaskStore.search` заменить блок CTE `rrf` (строки 269-272):

```python
        rrf AS (
            SELECT id, 1.0/(60+rank) AS s FROM bm25
            UNION ALL SELECT id, 1.0/(60+rank) AS s FROM ann
        )
```

на:

```python
        rrf AS (
            SELECT id, 1.0/(%(rrf_k)s::int + rank) AS s FROM bm25
            UNION ALL SELECT id, 1.0/(%(rrf_k)s::int + rank) AS s FROM ann
        )
```

и добавить ключ в `params` (строки 277-278):

```python
        params = {"q": _bm25_query(query_text), "vec": Vector(query_embedding),
                  "cand": candidates, "k": top_k, "rrf_k": RRF_K}
```

- [ ] **Step 6: Убрать второе объявление из `reviewer/retrieval/multiquery.py`**

Заменить строки 19-27:

```python
from reviewer.index.refs import base_ref
from reviewer.retrieval.retriever import (
    ContextPack, _dedupe_overlapping, _is_test_path,
)

log = logging.getLogger(__name__)

RRF_K = 60
"""Константа RRF — та же, что в store.hybrid_search и TaskStore.search."""
```

на:

```python
from reviewer.index.refs import base_ref
from reviewer.retrieval.retriever import (
    ContextPack, _dedupe_overlapping, _is_test_path,
)
from reviewer.rrf import RRF_K

log = logging.getLogger(__name__)
```

Сигнатура `rrf_merge(runs: list[list], k: int = RRF_K)` не меняется. Реэкспорт не оформляется отдельно: имя остаётся доступным как `multiquery.RRF_K` просто потому, что импортировано, и этого достаточно — внешних потребителей у него нет.

- [ ] **Step 7: Запустить guard и убедиться, что он зелёный**

Run: `.venv/bin/pytest tests/test_rrf_k_single_source.py -v`

Expected: PASS, 3 passed.

- [ ] **Step 8: Прогнать соседние тесты ретрива и индекса**

Run: `.venv/bin/pytest tests/retrieval tests/index tests/tasks -q`

Expected: всё зелёное. Значение k не изменилось, поэтому ни один тест ранжирования не должен сдвинуться. Красный тест здесь — регрессия, а не ожидаемое следствие: остановиться и разобраться, не подгоняя тест.

- [ ] **Step 9: Прогнать весь unit-набор и линт**

Run: `.venv/bin/pytest -q`
Expected: зелёно, ни одного упавшего.

Run: `.venv/bin/ruff check reviewer tests`
Expected: чисто.

- [ ] **Step 10: Коммит**

```bash
git add reviewer/rrf.py reviewer/index/store.py reviewer/tasks/store.py \
        reviewer/retrieval/multiquery.py tests/test_rrf_k_single_source.py
git commit -m "feat(retrieval): единственное объявление константы RRF

Литерал 60 уходит из обоих SQL: значение приезжает именованным
параметром %(rrf_k)s::int из нового reviewer/rrf.py. Модуль в корне
пакета, потому что оба store лежат ниже retrieval и импорт в обратную
сторону развернул бы направление зависимости.

Guard проверяет фактически переданные драйверу sql и params, а не текст
исходника, и красит возврат числового литерала в знаменатель."
```

---

### Task 2: Замер EXPLAIN и фиксация в спеке

Отдельный гейт: рецензент может принять параметризацию, но отвергнуть доказательство. Требует живой базы, поэтому не смешивается с unit-работой Задачи 1.

**Files:**
- Modify: `docs/superpowers/specs/2026-08-24-pri-267-rrf-k-single-source-design.md` (добавить раздел «Замер EXPLAIN» перед разделом «Документация»)
- Временный скрипт замера пишется в scratchpad и **не коммитится**.

**Interfaces:**
- Consumes: `reviewer.rrf.RRF_K` и обновлённый `ChunkStore.hybrid_search` из Задачи 1.
- Produces: раздел «Замер EXPLAIN» в спеке — на него ссылается критерий приёмки 3.

- [ ] **Step 1: Поднять локальную инфраструктуру**

Run: `reviewer start`

Expected: ParadeDB и Neo4j подняты, healthcheck зелёный. Если команда недоступна — `docker compose up -d`.

Проверить, что индекс непустой:

Run: `reviewer status --json`
Expected: `chunks` заметно больше нуля (на момент планирования — 7705).

- [ ] **Step 2: Написать скрипт замера**

Создать во временном каталоге (НЕ в репозитории) файл `explain_rrf.py`:

```python
"""Разовый замер PRI-267: план запроса с литералом против плана с параметром.

Эмбеддинг НЕ считается через Voyage — берётся готовый вектор из таблицы
chunks: замер про план запроса, а не про качество выдачи, и тратить квоту
незачем.
"""
import psycopg

from reviewer.config.settings import Settings
from reviewer.rrf import RRF_K

WHERE = ("repo=%(repo)s AND "
         "((ref=%(base)s AND NOT (path = ANY(%(changed)s))) OR ref=%(overlay)s)")

TEMPLATE = """
WITH bm25 AS (
    SELECT id, RANK() OVER (ORDER BY pdb.score(id) DESC) AS rank
    FROM chunks
    WHERE text @@@ %(q)s AND {where}
    ORDER BY pdb.score(id) DESC LIMIT %(cand)s
),
ann AS (
    SELECT id, (embedding <=> %(vec)s) AS dist,
           RANK() OVER (ORDER BY embedding <=> %(vec)s) AS rank
    FROM chunks
    WHERE {where}
    ORDER BY embedding <=> %(vec)s LIMIT %(cand)s
),
rrf AS (
    SELECT id, {denom} AS s FROM bm25
    UNION ALL
    SELECT id, {denom} AS s FROM ann
)
SELECT c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text,
       SUM(r.s) AS score,
       MIN(a.dist) AS ann_dist,
       bool_or(b.id IS NOT NULL) AS bm25_hit
FROM rrf r JOIN chunks c USING (id)
LEFT JOIN ann a ON a.id = c.id
LEFT JOIN bm25 b ON b.id = c.id
GROUP BY c.id, c.path, c.symbol_fqn, c.kind, c.start_line, c.end_line, c.text
ORDER BY score DESC LIMIT %(k)s
"""

LITERAL = TEMPLATE.format(where=WHERE, denom=f"1.0/({RRF_K}+rank)")
PARAM = TEMPLATE.format(where=WHERE, denom="1.0/(%(rrf_k)s::int + rank)")


def main() -> None:
    dsn = Settings().pg_dsn
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT repo, ref, embedding FROM chunks "
            "WHERE ref LIKE 'base:%' LIMIT 1").fetchone()
        assert row is not None, "индекс пуст — сначала выполни reviewer index"
        repo, ref, vec = row
        base = {"repo": repo, "q": "rrf", "vec": vec, "overlay": "pr:0",
                "changed": [], "cand": 50, "k": 15, "base": ref}
        for name, sql, params in (
            ("ЛИТЕРАЛ", LITERAL, base),
            ("ПАРАМЕТР", PARAM, {**base, "rrf_k": RRF_K}),
        ):
            print(f"===== {name} =====")
            for line in conn.execute(
                    "EXPLAIN (ANALYZE, BUFFERS) " + sql, params).fetchall():
                print(line[0])
            print()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Снять замер**

Run: `.venv/bin/python /tmp/explain_rrf.py 2>&1 | tee /tmp/explain_rrf.txt`

Expected: два блока плана. Сравнивать надо **структуру** (узлы, порядок соединений, использование индексов) — не абсолютные времена: они шумят от прогрева кэша.

- [ ] **Step 4: Записать замер в спеку**

Добавить в `docs/superpowers/specs/2026-08-24-pri-267-rrf-k-single-source-design.md` раздел `## Замер EXPLAIN` **перед** разделом `## Документация`. Раздел содержит: дату замера, версию PostgreSQL (`SELECT version()`), число чанков в индексе, оба плана целиком в блоках кода и один вывод одной фразой — совпала ли структура плана.

**Если структура плана деградировала** (появился seq scan там, где был index scan; изменился порядок соединений): остановиться, не продолжать Задачу 3 и доложить пользователю. Спека предусматривает запасную ветку — откат на guard-тест поверх трёх литералов. Это решение пользователя, не исполнителя.

- [ ] **Step 5: Коммит**

```bash
git add docs/superpowers/specs/2026-08-24-pri-267-rrf-k-single-source-design.md
git commit -m "docs(spec): замер EXPLAIN PRI-267 — план запроса не изменился"
```

(Если план изменился — сообщение коммита должно говорить это прямо, а не обещать обратное.)

---

### Task 3: Документация и пересборка манифестов плагина

Отдельный гейт: рецензент оценивает формулировки и синхронность сгенерированных артефактов, а не код.

**Files:**
- Modify: `CLAUDE.md:422-426`
- Modify: `plugin/skills/solve-task/references/brief-format.md:35`
- Modify: сгенерированные манифесты (`.codex-plugin`, `plugin/.codex-plugin`, `plugin/.claude-plugin`, `plugin/assets`) — их переписывает скрипт, руками не трогать.

**Interfaces:**
- Consumes: `reviewer/rrf.py` и параметризованные SQL из Задачи 1; вывод замера из Задачи 2.
- Produces: ничего, чем пользуются другие задачи. Финальная задача плана.

- [ ] **Step 1: Переписать абзац долга в `CLAUDE.md`**

Заменить текст (строки 422-426):

```
  Долг, который стоит знать: константа RRF `k = 60` живёт в **трёх** независимых объявлениях —
  питоновская `RRF_K` в `multiquery.py` и по литералу в SQL `index/store.py::hybrid_search` и
  `tasks/store.py::search`. SQL не импортируется, поэтому питоновской копии не избежать, но ни один
  тест эти три значения не связывает: расхождение `k` между слиянием подзапросов и RRF внутри одного
  гибрида пройдёт молча.
```

на:

```
  Константа RRF объявлена ровно один раз (PRI-267): `RRF_K` в `reviewer/rrf.py`, а оба SQL
  (`index/store.py::hybrid_search`, `tasks/store.py::search`) берут значение именованным
  параметром `%(rrf_k)s::int` — числового литерала в знаменателе нет вовсе, поэтому
  расхождение не обнаруживается, а невозможно. Неочевидны три вещи. Модуль лежит в **корне
  пакета**, а не рядом с `rrf_merge`: оба store ниже `retrieval` (`multiquery` уже импортирует
  `index.refs`), и импорт в обратную сторону развернул бы направление зависимости. Каст
  `::int` не косметика: без него тип параметра выводится из контекста сложения и может
  разойтись между simple- и prepared-протоколом psycopg. Guard
  `tests/test_rrf_k_single_source.py` смотрит на **фактически переданные драйверу** sql и
  params, а не на текст исходника, и требует ровно двух плейсхолдеров на каждый store —
  подставленная одна ветка CTE из двух разъехалась бы так же молча, как прежний литерал.
```

- [ ] **Step 2: Убрать числовую копию k из промпта плагина**

В `plugin/skills/solve-task/references/brief-format.md` заменить фрагмент строки 35:

```
   `search_tasks`'s `score` is an RRF rank score (`SUM(1/(60+rank))`, ≈0.016–0.033), NOT comparable
```

на:

```
   `search_tasks`'s `score` is an RRF rank score (`SUM(1/(k+rank))`, k from `reviewer/rrf.py::RRF_K`, ≈0.016–0.033), NOT comparable
```

Число из промпта убирается совсем, а не синхронизируется: копия, которой нет, разъехаться не может.

- [ ] **Step 3: Пересобрать манифесты плагина**

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py`

Expected: скрипт переписывает манифесты и завершается кодом 0.

- [ ] **Step 4: Проверить синхронность манифестов**

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py --check`
Expected: код возврата 0, без сообщения о рассинхронизации.

- [ ] **Step 5: Прогнать полный unit-набор и линт**

Run: `.venv/bin/pytest -q`
Expected: зелёно. Особенно важны `tests/install/` (payload-digest) и `tests/skills/` (сборка промптов).

Run: `.venv/bin/ruff check reviewer tests`
Expected: чисто.

- [ ] **Step 6: Коммит**

```bash
git add CLAUDE.md plugin/skills/solve-task/references/brief-format.md \
        .codex-plugin plugin/.codex-plugin plugin/.claude-plugin plugin/assets
git commit -m "docs: константа RRF объявлена один раз — долг PRI-267 закрыт

Абзац в CLAUDE.md описывал долг как открытый и утверждал, что питоновской
копии не избежать: после параметризации это неверно. Числовая копия k
убрана и из промпта скилла — копии, которой нет, нечему разъезжаться.
Манифесты плагина пересобраны в том же коммите."
```

---

## Приёмка

- [ ] **Критерий 1** (изменение k красит тест): выполнен в перевёрнутом виде — мест больше не три, а одно. Проверка: вернуть литерал `1.0/(60+rank)` в одну ветку CTE любого store → `tests/test_rrf_k_single_source.py` краснеет на `_LITERAL` и на `sql.count(_PLACEHOLDER) == 2`. Вернуть как было.
- [ ] **Критерий 2** (ранжирование не меняется): `.venv/bin/pytest -q` зелёный целиком; `reviewer search "token verification"` до и после правки даёт тот же порядок результатов.
- [ ] **Критерий 3** (план не деградировал): раздел «Замер EXPLAIN» в спеке содержит оба плана и вывод.
- [ ] Документация не врёт: в `CLAUDE.md` нет утверждения про три объявления; в `plugin/` нет числовой копии `k`.
- [ ] `.venv/bin/python scripts/update_codex_plugin_manifest.py --check` возвращает 0.
