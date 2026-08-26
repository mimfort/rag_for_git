# PRI-261 — Контекстное ядро как знаменатель метрики брифа: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить `context-recall` — вторую метрику качества брифа РЯДОМ с `core-recall`, чей знаменатель выводится из графа кода (файлы, которые надо прочитать, а не изменить), и закрыть числом вопрос «различает ли метрика глубину фрагмента».

**Architecture:** Чистый вывод ядра живёт в `reviewer/metrics/brief_quality/context_core.py` и принимает обход графа инъекцией — ровно как `ground_truth.py` принимает `GitRunner`. Ввод-вывод (Cypher, git, tree-sitter) остаётся в `eval/solve_task_metrics/`. Поля метрики добавляются к `TaskQuality`/`QualityAggregate` аддитивно, с необязательным параметром `context_core`, поэтому онлайн-путь и числа приёмок PRI-255…259 не двигаются.

**Tech Stack:** Python 3.12, pytest, Neo4j (`GraphStore`, driver `neo4j`), tree-sitter (`reviewer/index/chunker.py`), git через инъектируемый `GitRunner`.

**Spec:** `docs/superpowers/specs/2026-08-19-pri-261-context-core-denominator-design.md`

## Global Constraints

- Язык кода, комментариев и докстрингов — **русский** (соглашение репозитория).
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`, упоминаний Claude).
- `reviewer/metrics/brief_quality/**` остаётся **чистым**: без git, без БД, без сети, без Neo4j. Обход графа приходит только инъекцией.
- `reviewer/**` не импортирует `eval/**` — инвариант направления зависимости, охраняется `tests/metrics/test_reexport_guard.py::test_production_core_does_not_import_eval`.
- `eval/solve_task_metrics/` — только **ре-экспорт** формул из `reviewer/metrics/brief_quality/`, второй копии быть не должно.
- Unit-тестам запрещены внешние и localhost-сокеты. Любой тест, которому нужен Neo4j или Postgres, обязан иметь `@pytest.mark.integration`.
- Дефолты `CodeSectionLimits` (`max_files=20`, `max_chunks_per_file=1`, `chars_per_file=975`) в этой задаче **не меняются**.
- Схема таблицы `brief_quality` в этой задаче **не меняется** — метрика офлайн-only (явный выбор по критерию 6).
- Прогон unit-тестов: `.venv/bin/pytest -q`. Integration: `.venv/bin/pytest -q -m integration`.

---

### Task 1: Чистый вывод контекстного ядра

**Files:**
- Create: `reviewer/metrics/brief_quality/context_core.py`
- Create: `eval/solve_task_metrics/context_core.py`
- Test: `tests/metrics/test_context_core.py`
- Modify: `tests/metrics/test_reexport_guard.py`

**Interfaces:**
- Consumes: `reviewer.metrics.brief_quality.classify.is_core_production_path`
- Produces:
  - `Traversal = Callable[[list[str]], set[str]]`
  - `node_paths(node_ids: Iterable[str]) -> set[str]`
  - `derive_context_core(seed_ids: Iterable[str], changed_core: Iterable[str], traverse: Traversal) -> set[str]`

- [ ] **Step 1: Write the failing test**

Создать `tests/metrics/test_context_core.py`:

```python
"""Чистый вывод контекстного ядра: без графа, обход инъектируется."""
from __future__ import annotations

from reviewer.metrics.brief_quality.context_core import (
    derive_context_core,
    node_paths,
)


def test_node_paths_splits_node_ids():
    assert node_paths(["a/b.py#F.m", "c.py#g"]) == {"a/b.py", "c.py"}


def test_node_paths_ignores_ids_without_separator():
    """node_id без '#' — не символ; догадываться о пути по нему нельзя."""
    assert node_paths(["reviewer/x.py", "reviewer/y.py#f"]) == {"reviewer/y.py"}


def test_empty_seeds_do_not_call_traversal():
    """Пустые сиды дают пустое ядро и НЕ ходят в граф: пустой запрос в Neo4j
    стоит round-trip и на исторических задачах случается регулярно."""
    calls = []

    def traverse(ids):
        calls.append(ids)
        return set()

    assert derive_context_core([], {"reviewer/a.py"}, traverse) == set()
    assert calls == []


def test_derives_core_paths_of_neighbours():
    def traverse(ids):
        assert ids == ["reviewer/a.py#f"]
        return {"reviewer/b.py#g", "reviewer/c.py#h"}

    result = derive_context_core(
        ["reviewer/a.py#f"], {"reviewer/a.py"}, traverse
    )
    assert result == {"reviewer/b.py", "reviewer/c.py"}


def test_subtracts_changed_core():
    """Файл, который задача И читала, И меняла, принадлежит старому знаменателю:
    в контекстном ядре он был бы посчитан дважды."""
    def traverse(ids):
        return {"reviewer/a.py#f", "reviewer/b.py#g"}

    result = derive_context_core(
        ["reviewer/a.py#f"], {"reviewer/a.py"}, traverse
    )
    assert result == {"reviewer/b.py"}


def test_filters_non_core_paths():
    """Тесты, доки и eval/ вне ядра — та же линейка, что у core-recall."""
    def traverse(ids):
        return {"tests/test_a.py#t", "docs/x.md#d", "eval/y.py#e",
                "reviewer/b.py#g"}

    result = derive_context_core(["reviewer/a.py#f"], set(), traverse)
    assert result == {"reviewer/b.py"}


def test_seeds_passed_sorted_for_determinism():
    """Порядок сидов детерминирован: снимок обязан воспроизводиться."""
    seen = []

    def traverse(ids):
        seen.append(list(ids))
        return set()

    derive_context_core({"b.py#g", "a.py#f"}, set(), traverse)
    assert seen == [["a.py#f", "b.py#g"]]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/metrics/test_context_core.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.metrics.brief_quality.context_core'`

- [ ] **Step 3: Write minimal implementation**

Создать `reviewer/metrics/brief_quality/context_core.py`:

```python
"""Контекстное ядро задачи: файлы, которые надо ПРОЧИТАТЬ, а не изменить.

Знаменатель core-recall — только изменённые файлы, поэтому контракт, соседний
адаптер или образец для нового кода в него не входят: recall их не штрафует,
а precision — штрафует. Контекстное ядро выводится из графа: соседи символов,
которых коснулся дифф задачи.

Модуль ЧИСТЫЙ. Обход графа приходит инъекцией (`Traversal`), а не импортом
GraphStore: чистота brief_quality есть условие того, что офлайн и онлайн
меряют одной линейкой — тот же приём, которым ground_truth.py принимает
GitRunner.
"""
from __future__ import annotations

from typing import Callable, Iterable

from reviewer.metrics.brief_quality.classify import is_core_production_path

Traversal = Callable[[list], set]
"""Обход графа: отсортированный список сид-символов → множество соседних node_id."""


def node_paths(node_ids: Iterable[str]) -> set:
    """Пути символов. node_id = "path#fqn"; идентификатор без '#' пропускается.

    Пропуск, а не разбор до первого слэша: строка без разделителя — это не
    символ, и достраивать из неё путь значило бы завышать ядро догадкой.
    """
    return {nid.split("#", 1)[0] for nid in node_ids if "#" in nid}


def derive_context_core(
    seed_ids: Iterable[str],
    changed_core: Iterable[str],
    traverse: Traversal,
) -> set:
    """Контекстное ядро: core-пути соседей сид-символов минус изменённое ядро.

    Вычитание обязательно: файл, который задача и читала, и меняла, уже
    посчитан знаменателем core-recall, и в обоих знаменателях сразу он дал бы
    двойной вес.
    """
    seeds = sorted(seed_ids)
    if not seeds:
        return set()
    neighbours = traverse(seeds)
    paths = {p for p in node_paths(neighbours) if is_core_production_path(p)}
    return paths - set(changed_core)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest -q tests/metrics/test_context_core.py`
Expected: PASS (7 passed)

- [ ] **Step 5: Add the re-export and extend the guard test**

Создать `eval/solve_task_metrics/context_core.py`:

```python
"""Ре-экспорт вывода контекстного ядра из reviewer/ (PRI-261)."""
from reviewer.metrics.brief_quality.context_core import (  # noqa: F401
    Traversal,
    derive_context_core,
    node_paths,
)
```

В `tests/metrics/test_reexport_guard.py` добавить импорт рядом с существующими:

```python
from eval.solve_task_metrics import context_core as eval_context_core
from reviewer.metrics.brief_quality import context_core as prod_context_core
```

и в конец `test_eval_reexports_production_objects` — две строки:

```python
    assert eval_context_core.derive_context_core is prod_context_core.derive_context_core
    assert eval_context_core.node_paths is prod_context_core.node_paths
```

- [ ] **Step 6: Run the guard test**

Run: `.venv/bin/pytest -q tests/metrics/`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add reviewer/metrics/brief_quality/context_core.py \
        eval/solve_task_metrics/context_core.py \
        tests/metrics/test_context_core.py \
        tests/metrics/test_reexport_guard.py
git commit -m "feat(metrics): чистый вывод контекстного ядра задачи из графа"
```

---

### Task 2: Аддитивные поля метрики

**Files:**
- Modify: `reviewer/metrics/brief_quality/recall.py:32-88`
- Test: `tests/metrics/test_recall.py`

**Interfaces:**
- Consumes: ничего нового.
- Produces:
  - `evaluate_task(task_key: str, predicted: set, expected: set, expected_core: set, context_core: set | None = None) -> TaskQuality`
  - `TaskQuality` новые поля: `context_core: int = 0`, `hit_context: int = 0`, `context_recall: float | None = None`, `union_precision: float | None = None`
  - `QualityAggregate` новые поля: `context_recall_median: float | None = None`, `context_n_measured: int = 0`, `no_context_measurement: int = 0`, `union_precision_median: float | None = None`

- [ ] **Step 1: Write the failing test**

Дописать в `tests/metrics/test_recall.py`:

```python
def test_context_core_absent_leaves_new_fields_neutral():
    """Без context_core поведение тождественно доPRI-261: это и есть механизм,
    которым числа приёмок PRI-255…259 остаются сравнимыми без пересчёта."""
    row = recall.evaluate_task(
        "PRI-1", {"reviewer/a.py"}, {"reviewer/a.py"}, {"reviewer/a.py"}
    )
    assert row.context_recall is None
    assert row.union_precision is None
    assert row.context_core == 0
    assert row.hit_context == 0


def test_context_recall_counts_read_only_files():
    row = recall.evaluate_task(
        "PRI-1",
        predicted={"reviewer/a.py", "reviewer/b.py"},
        expected={"reviewer/a.py"},
        expected_core={"reviewer/a.py"},
        context_core={"reviewer/b.py", "reviewer/c.py"},
    )
    assert row.context_core == 2
    assert row.hit_context == 1
    assert row.context_recall == 0.5


def test_empty_context_denominator_is_none_not_zero():
    """Пустое контекстное ядро — «нет точки измерения», по образцу
    empty_core_denominator; ноль занижал бы медиану систематически."""
    row = recall.evaluate_task(
        "PRI-1", {"reviewer/a.py"}, {"reviewer/a.py"}, {"reviewer/a.py"},
        context_core=set(),
    )
    assert row.context_recall is None


def test_union_precision_is_never_below_old_precision():
    """Файл, который надо было ПРОЧИТАТЬ, перестаёт считаться шумом.
    Объединение идёт по expected (все изменённые), а не по expected_core:
    по одному ядру новая precision могла бы оказаться НИЖЕ старой."""
    row = recall.evaluate_task(
        "PRI-1",
        predicted={"reviewer/a.py", "reviewer/b.py"},
        expected={"reviewer/a.py", "tests/test_a.py"},
        expected_core={"reviewer/a.py"},
        context_core={"reviewer/b.py"},
    )
    assert row.precision == 0.5
    assert row.union_precision == 1.0


def test_aggregate_reports_context_medians_and_gaps():
    rows = [
        recall.evaluate_task("A", {"reviewer/b.py"}, {"reviewer/a.py"},
                             {"reviewer/a.py"}, context_core={"reviewer/b.py"}),
        recall.evaluate_task("B", {"reviewer/a.py"}, {"reviewer/a.py"},
                             {"reviewer/a.py"}, context_core=set()),
    ]
    agg = recall.aggregate(rows)
    assert agg.context_n_measured == 1
    assert agg.no_context_measurement == 1
    assert agg.context_recall_median == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/metrics/test_recall.py`
Expected: FAIL — `TypeError: evaluate_task() got an unexpected keyword argument 'context_core'`

- [ ] **Step 3: Write minimal implementation**

В `reviewer/metrics/brief_quality/recall.py` — в `TaskQuality` дописать поля после `precision`:

```python
    context_core: int = 0
    hit_context: int = 0
    context_recall: float | None = None
    union_precision: float | None = None
```

В `QualityAggregate` дописать после `bulk_n_measured`:

```python
    context_recall_median: float | None = None
    context_n_measured: int = 0
    no_context_measurement: int = 0
    union_precision_median: float | None = None
```

Заменить сигнатуру и хвост `evaluate_task`:

```python
def evaluate_task(task_key: str, predicted: set, expected: set,
                  expected_core: set, context_core: set | None = None) -> TaskQuality:
    """Посчитать метрики одной задачи; core_recall=None при пустом ядре.

    context_core необязателен: без него строка тождественна доPRI-261, и именно
    это оставляет числа приёмок PRI-255…259 сравнимыми без пересчёта (критерий 3).
    """
    hit_core = predicted & expected_core
    hit_raw = predicted & expected
    row = TaskQuality(
        task_key=task_key,
        expected=len(expected),
        expected_core=len(expected_core),
        predicted=len(predicted),
        hit_core=len(hit_core),
    )
    row.core_recall = len(hit_core) / len(expected_core) if expected_core else None
    row.raw_recall = len(hit_raw) / len(expected) if expected else None
    row.precision = len(hit_raw) / len(predicted) if predicted else None
    if context_core is not None:
        hit_context = predicted & context_core
        row.context_core = len(context_core)
        row.hit_context = len(hit_context)
        row.context_recall = (
            len(hit_context) / len(context_core) if context_core else None
        )
        # Объединение по expected, а не по expected_core: новая precision обязана
        # быть надмножеством старой, иначе рычаг читается наоборот.
        union = set(expected) | set(context_core)
        row.union_precision = (
            len(predicted & union) / len(predicted) if predicted else None
        )
    return row
```

В `aggregate` дописать перед `return agg`:

```python
    context = [r for r in rows if r.context_recall is not None]
    agg.context_n_measured = len(context)
    agg.no_context_measurement = len(rows) - len(context)
    if context:
        agg.context_recall_median = statistics.median(
            [r.context_recall for r in context]
        )
    union_values = [r.union_precision for r in rows if r.union_precision is not None]
    if union_values:
        agg.union_precision_median = statistics.median(union_values)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q tests/metrics/ tests/services/test_brief_quality.py tests/eval/`
Expected: PASS — существующие тесты онлайн-съёма и replay не краснеют, потому что параметр необязателен.

- [ ] **Step 5: Commit**

```bash
git add reviewer/metrics/brief_quality/recall.py tests/metrics/test_recall.py
git commit -m "feat(metrics): context-recall и union-precision рядом с core-recall"
```

---

### Task 3: Направленный обход графа

**Files:**
- Modify: `reviewer/graph/store.py` (новый метод после `expand`, около строки 85)
- Test: `tests/graph/test_store.py`

**Interfaces:**
- Consumes: ничего нового.
- Produces: `GraphStore.outgoing_neighbors(repo: str, node_ids: list, *, branch: str = "") -> set`

- [ ] **Step 1: Write the failing test**

Дописать в `tests/graph/test_store.py`:

```python
@pytest.mark.integration
def test_outgoing_neighbors_is_directed(graph_store):
    """Только исходящие рёбра: контекстное ядро отвечает на вопрос «что читать,
    чтобы НАПИСАТЬ», а не «кого я сломаю» — это разные вопросы."""
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes("test/repo", ["a.py#g", "a.py#f", "a.py#h"])
    graph_store.upsert_edges("test/repo", [
        ("a.py#g", "CALLS", "a.py#f"),
        ("a.py#h", "CALLS", "a.py#g"),
    ])
    out = graph_store.outgoing_neighbors("test/repo", ["a.py#g"])
    assert out == {"a.py#f"}


@pytest.mark.integration
def test_outgoing_neighbors_includes_implements(graph_store):
    """IMPLEMENTS идёт наследник→база: соседний адаптер и контракт — ровно тот
    случай, ради которого метрика заведена."""
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes("test/repo", ["a.py#Child", "b.py#Base"])
    graph_store.upsert_edges("test/repo", [
        ("a.py#Child", "IMPLEMENTS", "b.py#Base"),
    ])
    out = graph_store.outgoing_neighbors("test/repo", ["a.py#Child"])
    assert out == {"b.py#Base"}


@pytest.mark.integration
def test_outgoing_neighbors_empty_ids(graph_store):
    assert graph_store.outgoing_neighbors("test/repo", []) == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose up -d && .venv/bin/pytest -q -m integration tests/graph/test_store.py`
Expected: FAIL — `AttributeError: 'GraphStore' object has no attribute 'outgoing_neighbors'`

- [ ] **Step 3: Write minimal implementation**

В `reviewer/graph/store.py`, сразу после метода `expand`:

```python
    def outgoing_neighbors(self, repo: str, node_ids: list, *,
                           branch: str = "") -> set:
        """Соседи по ИСХОДЯЩИМ CALLS/IMPLEMENTS на один хоп (PRI-261).

        Отличие от expand: тот ненаправленный и многохоповый. Здесь направление
        существенно — контекстное ядро отвечает «что надо прочитать, чтобы
        написать этот код», то есть вызываемое и наследуемое, а не вызывающее.
        На замере ненаправленный обход давал 60 файлов медианы против 14.5
        у направленного, то есть треть репозитория.
        """
        if not node_ids:
            return set()
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid "
            "MATCH (s:Symbol {repo: $repo, branch: $branch, id: sid})"
            "-[:CALLS|IMPLEMENTS]->(n:Symbol {repo: $repo, branch: $branch}) "
            "RETURN DISTINCT n.id AS id",
            ids=list(node_ids), repo=repo, branch=branch)
        return {r["id"] for r in records}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest -q -m integration tests/graph/test_store.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/graph/store.py tests/graph/test_store.py
git commit -m "feat(graph): направленный однохоповый обход CALLS/IMPLEMENTS"
```

---

### Task 4: Вывод сид-символов из диффа исторических мержей

**Files:**
- Create: `eval/solve_task_metrics/context_seeds.py`
- Test: `tests/eval/test_context_seeds.py`

**Interfaces:**
- Consumes: `eval.solve_task_metrics.ground_truth.GitRunner`, `reviewer.index.chunker.chunk_python`, `reviewer.metrics.brief_quality.classify.is_core_production_path`
- Produces:
  - `parse_hunk_ranges(diff_text: str) -> list` — список `(start, end)` по ПРАВОЙ стороне
  - `seeds_for_merge(sha: str, core_paths: set, run_git) -> set` — множество `path#fqn`
  - `collect_seeds(truth, run_git) -> set` — объединение по всем PR-мержам задачи

Почему сиды берутся из ИСТОРИЧЕСКОГО коммита, а не из сегодняшних чанков: номера строк
диффа относятся к коммиту мержа, а чанки индексированы на сегодняшнем `dev`. Наложение
исторических номеров на сегодняшние диапазоны попадает не в те символы у любого файла,
который с тех пор менялся. Поэтому файл читается на его собственном коммите
(`git show sha:path`) и разбирается тем же tree-sitter-чанкером, что и индекс, — а с
сегодняшним графом сшивается уже по ИМЕНИ символа, не по строке.

- [ ] **Step 1: Write the failing test**

Создать `tests/eval/test_context_seeds.py`:

```python
"""Сиды контекстного ядра: разбор hunk'ов и сшивка с символами коммита."""
from __future__ import annotations

import pytest

from eval.solve_task_metrics import context_seeds

DIFF = """diff --git a/reviewer/a.py b/reviewer/a.py
index 111..222 100644
--- a/reviewer/a.py
+++ b/reviewer/a.py
@@ -1,0 +1,2 @@ def f():
+    x = 1
+    y = 2
@@ -5 +5 @@ def g():
-    old()
+    new()
"""

SOURCE = """def f():
    pass


def g():
    pass
"""


def test_parse_hunk_ranges_reads_right_side():
    assert context_seeds.parse_hunk_ranges(DIFF) == [(1, 2), (5, 5)]


def test_parse_hunk_ranges_pure_deletion_marks_the_seam():
    """У чистого удаления длина правой стороны 0; сид — строка стыка, иначе
    удалённый код не имел бы сида вовсе и задача теряла бы знаменатель."""
    diff = "@@ -10,3 +9,0 @@ def f():\n-    a()\n"
    assert context_seeds.parse_hunk_ranges(diff) == [(9, 9)]


def test_parse_hunk_ranges_defaults_length_to_one():
    assert context_seeds.parse_hunk_ranges("@@ -1 +7 @@\n+x\n") == [(7, 7)]


def test_seeds_for_merge_maps_ranges_to_symbols():
    calls = []

    def run_git(args):
        calls.append(args)
        if args[0] == "diff":
            return DIFF
        if args[0] == "show":
            return SOURCE
        raise AssertionError(args)

    seeds = context_seeds.seeds_for_merge("deadbeef", {"reviewer/a.py"}, run_git)
    assert seeds == {"reviewer/a.py#f", "reviewer/a.py#g"}


def test_seeds_for_merge_skips_non_core_paths():
    """Тесты и доки в сиды не идут: линейка та же, что у core-recall."""
    def run_git(args):
        raise AssertionError("git не должен вызываться для не-core путей")

    assert context_seeds.seeds_for_merge("x", {"tests/test_a.py"}, run_git) == set()


def test_seeds_for_merge_survives_git_failure():
    """Файл, удалённый после мержа, git show не отдаст. Прогон корпуса не падает:
    у задачи просто меньше сидов, и это видно по их числу."""
    def run_git(args):
        if args[0] == "diff":
            return DIFF
        raise context_seeds.ground_truth.GitError("no such path")

    assert context_seeds.seeds_for_merge("x", {"reviewer/a.py"}, run_git) == set()


def test_seeds_for_merge_skips_unparsable_source():
    """Не-Python или битый файл не роняет прогон."""
    def run_git(args):
        return DIFF if args[0] == "diff" else "\x00\x01 not python"

    context_seeds.seeds_for_merge("x", {"reviewer/a.py"}, run_git)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/eval/test_context_seeds.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.solve_task_metrics.context_seeds'`

- [ ] **Step 3: Write minimal implementation**

Создать `eval/solve_task_metrics/context_seeds.py`:

```python
"""Сид-символы контекстного ядра: что дифф задачи реально трогал (PRI-261).

Сиды — НЕ все символы изменённых файлов. Замер: сидирование целыми файлами даёт
медиану 57 новых core-файлов (37 % репозитория, «мусорное ядро»), сидирование
затронутыми символами — 14.5 на том же графе и той же глубине. Разница вчетверо
решается здесь, а не выбором числа хопов.

Символы берутся на КОММИТЕ МЕРЖА, а не из сегодняшнего индекса: номера строк
диффа относятся к своему коммиту, и наложение их на сегодняшние диапазоны чанков
попадает не в те символы у любого файла, который с тех пор менялся. С сегодняшним
графом результат сшивается по имени символа, а не по строке.
"""
from __future__ import annotations

import re

from reviewer.index.chunker import chunk_python
from reviewer.metrics.brief_quality.classify import is_core_production_path

from . import ground_truth

# '@@ -a,b +c,d @@ [контекст]'. Интересует только правая сторона: сид — символ
# в состоянии ПОСЛЕ мержа, который сегодняшний граф и знает по имени.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_hunk_ranges(diff_text: str) -> list:
    """Диапазоны строк правой стороны диффа: [(start, end), ...].

    Чистое удаление (длина 0) даёт диапазон в одну строку на месте стыка:
    у удалённого кода иначе не было бы сида вовсе, и задача теряла бы часть
    знаменателя молча.
    """
    ranges: list = []
    for line in (diff_text or "").splitlines():
        match = _HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        length = int(match.group(2)) if match.group(2) is not None else 1
        if length == 0:
            ranges.append((start, start))
        else:
            ranges.append((start, start + length - 1))
    return ranges


def _symbols_at(path: str, source: str, ranges: list) -> set:
    """Символы файла, чьи диапазоны пересекаются с изменёнными строками."""
    try:
        chunks = chunk_python(path, source.encode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — не-Python или битый файл не роняет прогон
        return set()
    hits: set = set()
    for chunk in chunks:
        for start, end in ranges:
            if chunk.start_line <= end and chunk.end_line >= start:
                hits.add(f"{path}#{chunk.symbol_fqn}")
                break
    return hits


def seeds_for_merge(sha: str, core_paths: set, run_git) -> set:
    """Сид-символы одного PR-мержа по его core-путям."""
    seeds: set = set()
    for path in sorted(p for p in core_paths if is_core_production_path(p)):
        try:
            diff = run_git(["diff", "--unified=0", f"{sha}^1", sha, "--", path])
            source = run_git(["show", f"{sha}:{path}"])
        except ground_truth.GitError:
            # Путь удалён или недостижим на этом коммите: сидов меньше, прогон жив.
            continue
        ranges = parse_hunk_ranges(diff)
        if ranges:
            seeds |= _symbols_at(path, source, ranges)
    return seeds


def collect_seeds(truth, run_git) -> set:
    """Сид-символы задачи: объединение по всем её настоящим PR-мержам."""
    core_paths = {p for p in truth.changed if is_core_production_path(p)}
    seeds: set = set()
    for sha in truth.merge_shas:
        seeds |= seeds_for_merge(sha, core_paths, run_git)
    return seeds
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest -q tests/eval/test_context_seeds.py`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add eval/solve_task_metrics/context_seeds.py tests/eval/test_context_seeds.py
git commit -m "feat(eval): сид-символы контекстного ядра из диффа PR-мержей"
```

---

### Task 5: Провайдер обхода и строка replay

**Files:**
- Modify: `eval/solve_task_metrics/live.py` (новый метод в `LiveRetrieval`, после `code_multi`)
- Modify: `eval/solve_task_metrics/replay.py:20-21` (константа), `:55-70` (`_task_row`), `:71-103` (`_evaluate`), `:139-160` (цикл), `:158-208` (агрегат)
- Test: `tests/eval/test_replay.py`, `tests/eval/test_live_boundary.py`

**Interfaces:**
- Consumes: `context_core.derive_context_core`, `context_seeds.collect_seeds`, `GraphStore.outgoing_neighbors`, `recall.evaluate_task(..., context_core=...)`
- Produces:
  - `LiveRetrieval.neighbors(repo: str, branch: str, node_ids: list) -> set`
  - `replay.STATUS_EMPTY_CONTEXT = "empty_context_denominator"`
  - Новые ключи строки задачи: `context_status`, `context_core`, `hit_context`, `context_recall`, `union_precision`, `context_core_paths`
  - Новые ключи `snapshot["aggregate"]`: `context_recall_median`, `context_n_measured`, `no_context_measurement`, `union_precision_median`

- [ ] **Step 1: Write the failing test**

Дописать в `tests/eval/test_replay.py` (использовать существующие фикстуры и фейковый провайдер этого файла; если у фейка нет метода `neighbors`, добавить его туда же):

```python
def test_context_core_fields_present_in_row(tmp_path):
    """Контекстное ядро считается рядом с core, своим статусом и своими путями."""
    snap = _run(tmp_path, neighbors={"reviewer/b.py#g"})
    row = snap["tasks"][0]
    assert row["context_status"] == replay.STATUS_MEASURED
    assert row["context_core_paths"] == ["reviewer/b.py"]
    assert row["context_recall"] is not None


def test_empty_context_core_is_not_zero_recall(tmp_path):
    """Пустое контекстное ядро — отдельный статус и None, по образцу
    empty_core_denominator."""
    snap = _run(tmp_path, neighbors=set())
    row = snap["tasks"][0]
    assert row["context_status"] == replay.STATUS_EMPTY_CONTEXT
    assert row["context_recall"] is None


def test_aggregate_carries_context_medians(tmp_path):
    snap = _run(tmp_path, neighbors={"reviewer/b.py#g"})
    agg = snap["aggregate"]
    assert "context_recall_median" in agg
    assert "union_precision_median" in agg
    assert agg["context_n_measured"] >= 0
```

Здесь `_run(tmp_path, neighbors=...)` — локальный хелпер этого теста: он собирает
существующий фейковый провайдер файла, доопределяет ему `neighbors(...)`, возвращающий
переданное множество, и зовёт `replay.run_replay(...)` с уже используемыми в файле
аргументами. Если в файле уже есть аналогичный хелпер сборки снимка — переиспользовать
его, добавив параметр `neighbors`, а не заводить второй.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/eval/test_replay.py`
Expected: FAIL — `KeyError: 'context_status'`

- [ ] **Step 3: Add the provider method**

В `eval/solve_task_metrics/live.py`, в класс `LiveRetrieval` после `code_multi`:

```python
    def neighbors(self, repo: str, branch: str, node_ids: list) -> set:
        """Соседи символов по исходящим CALLS/IMPLEMENTS — обход для ядра (PRI-261).

        Ввод-вывод живёт здесь, а не в reviewer/metrics/brief_quality: тот модуль
        обязан оставаться чистым, иначе офлайн и онлайн перестают мерить одной
        линейкой. Отсутствующий граф даёт пустое множество, а не отказ прогона.
        """
        graph = self._components.graph
        if graph is None or not node_ids:
            return set()
        return graph.outgoing_neighbors(repo, sorted(node_ids), branch=branch)
```

- [ ] **Step 4: Wire the row and the aggregate**

В `eval/solve_task_metrics/replay.py`:

1. Рядом с существующими константами статусов (строки 20-21) добавить:

```python
STATUS_EMPTY_CONTEXT = "empty_context_denominator"
```

2. В `_task_row` дописать в словарь дефолтов:

```python
        "context_status": STATUS_EMPTY_CONTEXT,
        "context_core": 0,
        "hit_context": 0,
        "context_recall": None,
        "union_precision": None,
        "context_core_paths": [],
```

3. Сигнатуру `_evaluate` расширить и заменить вызов `evaluate_task` и сборку строки:

```python
def _evaluate(key: str, predicted: set, truth, run_git, context_core: set) -> dict:
    """Посчитать одну задачу той же линейкой, что build_snapshot."""
    existed_cache: dict = {}

    def existed(path: str) -> bool:
        if path not in existed_cache:
            existed_cache[path] = ground_truth.path_existed(
                truth.parent_ref, path, run_git
            )
        return existed_cache[path]

    expected_core = {
        path
        for path in truth.changed
        if classify.is_core_production_path(path) and existed(path)
    }
    row = recall.evaluate_task(
        key, predicted, truth.changed, expected_core, context_core=context_core
    )
    status = STATUS_MEASURED if expected_core else STATUS_EMPTY_CORE
    context_status = STATUS_MEASURED if context_core else STATUS_EMPTY_CONTEXT
    return _task_row(
        key,
        status,
        expected=row.expected,
        expected_core=row.expected_core,
        predicted=row.predicted,
        hit_core=row.hit_core,
        core_recall=row.core_recall,
        raw_recall=row.raw_recall,
        precision=row.precision,
        predicted_paths=sorted(predicted),
        expected_core_paths=sorted(expected_core),
        context_status=context_status,
        context_core=row.context_core,
        hit_context=row.hit_context,
        context_recall=row.context_recall,
        union_precision=row.union_precision,
        context_core_paths=sorted(context_core),
    )
```

4. В цикле `run_replay`, непосредственно перед вызовом `_evaluate`, вывести ядро:

```python
        try:
            seeds = context_seeds.collect_seeds(truth, run_git)
            core_now = derive_context_core(
                seeds,
                {p for p in truth.changed if classify.is_core_production_path(p)},
                lambda ids: provider.neighbors(target.repo, target.branch, ids),
            )
        except Exception:  # noqa: BLE001 — недоступный граф не роняет прогон корпуса
            core_now = set()
        row = _evaluate(key, predicted, truth, run_git, core_now)
```

и добавить импорты в шапку модуля:

```python
from reviewer.metrics.brief_quality.context_core import derive_context_core

from . import context_seeds
```

5. В сборку `quality` дописать поля, чтобы `aggregate` их увидел:

```python
            context_core=r["context_core"],
            hit_context=r["hit_context"],
            context_recall=r["context_recall"],
            union_precision=r["union_precision"],
```

6. В словарь `"aggregate"` дописать:

```python
            "context_recall_median": agg.context_recall_median,
            "context_n_measured": agg.context_n_measured,
            "no_context_measurement": agg.no_context_measurement,
            "union_precision_median": agg.union_precision_median,
```

7. Добавить `STATUS_EMPTY_CONTEXT` в список `STATUSES`, если он перечисляет статусы строки `status`. **Не добавлять**, если `STATUSES` используется для счётчика `snapshot["statuses"]` по полю `status`: `context_status` — отдельное поле, и смешение двух шкал в одном счётчике сделало бы суммы бессмысленными. Проверить по коду `STATUSES` перед правкой.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest -q tests/eval/ tests/metrics/`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add eval/solve_task_metrics/live.py eval/solve_task_metrics/replay.py \
        tests/eval/test_replay.py tests/eval/test_live_boundary.py
git commit -m "feat(eval): контекстное ядро в строке и агрегате replay"
```

---

### Task 6: Колонки отчёта

**Files:**
- Modify: `eval/solve_task_metrics/replay_report.py`
- Modify: `eval/solve_task_metrics/report.py:94`
- Test: `tests/eval/test_replay_report.py`

**Interfaces:**
- Consumes: ключи снимка из Task 5.
- Produces: колонки `ctx` (размер контекстного ядра), `ctx_hit`, `ctx_recall`, `u_prec` в таблице отчёта и строки агрегата `context_recall_median` / `union_precision_median`.

- [ ] **Step 1: Write the failing test**

Дописать в `tests/eval/test_replay_report.py`:

```python
def test_report_renders_context_columns():
    """Контекстные числа видны в отчёте: метрика, которую не печатают,
    не существует для читателя приёмки."""
    snap = _snapshot_with(
        context_recall_median=0.5, union_precision_median=0.8,
        context_n_measured=3, no_context_measurement=1,
    )
    text = replay_report.render(snap)
    assert "context_recall_median" in text or "ctx_recall" in text
    assert "0.5" in text
    assert "union_precision" in text or "u_prec" in text
```

`_snapshot_with(...)` — локальный хелпер: берёт минимальный снимок, уже используемый
в этом файле, и подменяет перечисленные ключи `aggregate`. Если такой хелпер уже есть —
переиспользовать его.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/eval/test_replay_report.py`
Expected: FAIL — assert по отсутствующей подстроке.

- [ ] **Step 3: Write minimal implementation**

Прочитать `eval/solve_task_metrics/replay_report.py` целиком, найти место, где рендерятся
строки агрегата (`core_recall_median`, `bulk_core_recall_median`, `precision_median`), и
дописать рядом, тем же форматом, четыре строки: `context_recall_median`,
`context_n_measured`, `no_context_measurement`, `union_precision_median`. Пустое значение
(`None`) печатать как `—`, а не как `0`: неопределённость и ноль — разные состояния, и
таблица обязана их различать.

В `eval/solve_task_metrics/report.py:94`, в форматирование строки задачи, добавить
колонки `context_core`, `hit_context`, `context_recall`, `union_precision` и соответствующие
заголовки в шапку таблицы, тем же стилем, что уже используется для `expected_core` /
`hit_core` / `precision`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q tests/eval/`
Expected: PASS

- [ ] **Step 5: Full unit suite and lint**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer eval tests`
Expected: тесты зелёные; ruff не отчитывается о НОВЫХ нарушениях (в репозитории ruff на `dev` не идеально чист — сравнивать со списком до правки, а не гнаться за repo-wide clean).

- [ ] **Step 6: Commit**

```bash
git add eval/solve_task_metrics/replay_report.py eval/solve_task_metrics/report.py \
        tests/eval/test_replay_report.py
git commit -m "feat(eval): колонки контекстного ядра в отчётах replay"
```

---

### Task 7: Замер, ручная сверка и отчёт

**Files:**
- Modify: `eval/replay_report.md` (новый раздел «Приёмка PRI-261» в конец)
- Modify: `CLAUDE.md` (абзац про онлайн-метрику PRI-249)

**Interfaces:**
- Consumes: всё предыдущее.
- Produces: раздел отчёта с числами и обновлённый абзац CLAUDE.md.

- [ ] **Step 1: Bring the infrastructure up and confirm the index**

```bash
docker compose up -d
reviewer status --json
```

Записать `indexed_sha` — **все прогоны этой задачи обязаны идти на одном значении**,
иначе дельта включает дрейф base-индекса.

- [ ] **Step 2: Run the corpus at the current default**

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths
```

(Точное имя варианта и флаги сверить с `eval/solve_task_metrics/__main__.py` — там же
показан синтаксис `--set` для оверрайда лимитов.)

Записать: `context_recall_median`, `context_n_measured`, `no_context_measurement`,
`union_precision_median`, `precision_median` (старая), медиану размера контекстного ядра.

- [ ] **Step 3: Manual eye-check on 5-10 tasks (acceptance criterion 2)**

Для 5-10 задач корпуса выписать `context_core_paths` из снимка и сверить глазами с тем,
что задача реально должна была читать (её спека, её дифф, её соседи). Зафиксировать
числом: сколько путей из ядра выглядят осмысленными, сколько мусорными. **Это гейт:**
если ядро на глаз мусорное, задача закрывается отрицательным результатом с числом, а
разделы отчёта пишутся про это, а не про метрику. Порог назвать ДО просмотра — предлагается
«не менее половины путей ядра осмысленны», и назвать его в отчёте явно.

- [ ] **Step 4: Run the three depth points**

Три прогона на том же `indexed_sha`, различающиеся только `chars_per_file`:

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths \
  --set code_section.max_files=20 --set code_section.chars_per_file=780
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths \
  --set code_section.max_files=20 --set code_section.chars_per_file=975
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths \
  --set code_section.max_files=20 --set code_section.chars_per_file=1300
```

Ожидание: три ТОЖДЕСТВЕННЫХ набора чисел. Это не открытие, а подтверждение доказательства
уровня кода (см. шаг 5). Если числа РАЗОШЛИСЬ — доказательство неверно, и это блокер:
остановиться, разобраться, почему `chars_per_file` влияет на набор путей, и только потом
писать отчёт.

- [ ] **Step 5: Write the report section**

Дописать в конец `eval/replay_report.md` раздел «## Приёмка PRI-261» со структурой:

1. **Что мерилось и зачем** — знаменатель контекста рядом с core-recall.
2. **Спайк: решает сидирование, а не глубина обхода** — таблица из спеки (60 / 57 / 14.5 / 31 / 3) с указанием `indexed_sha` и числа core-файлов репозитория (152).
3. **Ручная сверка** — числа шага 3 и названный заранее порог.
4. **Числа context-recall и union-precision** — шаг 2, рядом со старыми `core_recall_median` и `precision_median`, с явной оговоркой, что старые числа не пересчитывались.
5. **Вопрос глубины закрыт доказательством, а не замером** — сначала разбор
   `reviewer/retrieval/multiquery.py:274-292` (набор путей фиксирует `diversify_by_file` по
   `max_files`/`max_chunks_per_file`; `chars_per_file` применяется ПОСЛЕ, в `cap_block`, к
   тексту уже отобранных блоков; заголовок блока — первая строка и обрезкой не удаляется),
   ЗАТЕМ три совпавших числа как подтверждение. Вывод: ни одна метрика, считающая пути,
   глубину различать не может; пол `chars_per_file >= 975` остаётся инженерным решением,
   но теперь задокументирован как принципиально неизмеримый этим классом метрики.
   Дефолты `CodeSectionLimits` не изменены.
6. **Три оговорки** — `typing.Protocol` рёбер не даёт (структурная типизация); импорт не
   ребро (граф знает только вызовы); обход идёт по сегодняшнему графу, а не по состоянию
   репозитория на момент каждой исторической задачи.
7. **Выбор по критерию 6** — офлайн-only, схема `brief_quality` не менялась, основание:
   `bulk_n_measured = 4` слишком тонок, а обход добавил бы Neo4j-round-trip каждому
   `publish_review`.
8. **Процедура воспроизведения** — точные команды шагов 2 и 4.

- [ ] **Step 6: Update CLAUDE.md**

В абзаце «**Онлайн-метрика качества брифа solve-task (PRI-249)**» дописать в конец, что
рядом с `core-recall` существует офлайн-only `context-recall` (PRI-261): знаменатель —
контекстное ядро из графа (исходящие `CALLS`/`IMPLEMENTS` на один хоп от символов, которых
коснулся дифф), считается только эвал-харнессом, схема `brief_quality` его не хранит.
Назвать неочевидное: **решает сидирование, а не глубина обхода** (57 файлов при сидировании
целыми файлами против 14.5 при сидировании затронутыми символами), и **никакая
path-метрика не различает `chars_per_file`** — доказательство в `multiquery.py`, где
`cap_block` применяется после `diversify_by_file`.

- [ ] **Step 7: Verify and commit**

```bash
.venv/bin/pytest -q
git add eval/replay_report.md CLAUDE.md eval/replay_history.jsonl
git commit -m "docs(eval): приёмка PRI-261 — контекстное ядро рядом с core-recall"
```

(Если прогоны дописали `eval/replay_history.jsonl` — включить его в коммит; если нет —
убрать из `git add`.)

---

## Проверка критериев приёмки

| Критерий | Где закрыт |
|---|---|
| 1. Ядро выводится детерминированно из графа на заданном `indexed_sha` | Task 1 (чистый вывод), Task 3 (обход), Task 4 (сиды), Task 7 шаг 2 |
| 2. Спайк зафиксировал размер числом + ручная сверка 5-10 задач | Спека (таблица), Task 7 шаг 3 |
| 3. `context-recall` РЯДОМ, старые числа сравнимы | Task 2 (необязательный параметр), Task 5 (отдельные поля строки) |
| 4. Пустой знаменатель → отдельный статус и `None` | Task 2 (`context_recall=None`), Task 5 (`STATUS_EMPTY_CONTEXT`) |
| 5. Три точки глубины + ответ числом | Task 7 шаги 4-5 |
| 6. Онлайн-запись: выбор назван явно (офлайн-only) | Спека раздел 5, Task 7 шаг 5 пункт 7 |
| 7. Расчётное ядро одно, `eval/` — ре-экспорт, guard зелёный | Task 1 шаги 5-6 |
