# PRI-252: кросс-файловая фикция local-символов SCIP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать фиктивные CALLS-рёбра, которые возникают из-за резолвинга файл-скоупных `local N`-символов SCIP через глобальную карту, и сделать полноту графа наблюдаемой при индексации.

**Architecture:** `parse_scip` разделяет карту символов на глобальную и per-document; новый чистый модуль `reviewer/graph/metrics.py` считает рёбра по типам и детектирует просадку; счётчики предыдущего прогона ветки хранятся в новой nullable-колонке `index_meta.graph_edges`; `reviewer index` печатает разбивку и предупреждение.

**Tech Stack:** Python 3.11+, protobuf (SCIP), psycopg 3 (Postgres/ParadeDB), Click, pytest.

**Spec:** `docs/superpowers/specs/2026-08-14-scip-local-symbol-edges-design.md`

## Global Constraints

- Ветка работы: `feat/pri-252-scip-local-symbol-edges` (уже создана, спека и бриф закоммичены).
- Язык проекта — русский: комментарии, докстринги, сообщения CLI. Тесты и их докстринги тоже по-русски.
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`, упоминаний Claude).
- Unit-тестам запрещены Postgres, Neo4j, localhost-сокеты и внешняя сеть. Любой тест с реальной инфраструктурой обязан иметь `@pytest.mark.integration`.
- Unit-прогон: `.venv/bin/pytest -q`. Integration-прогон: `.venv/bin/pytest -q -m integration` (нужна поднятая тестовая инфраструктура).
- Миграции схемы только аддитивные и идемпотентные (`ADD COLUMN IF NOT EXISTS`) — тот же стиль, что уже в `reviewer/index/schema.sql`.
- Порог просадки — константа модуля, env-слоя не заводить.
- `git push` и создание PR требуют отдельного подтверждения пользователя; в рамках этого плана не выполняются.

---

### Task 1: Скоупинг `local`-символов по документу в `parse_scip`

Корень дефекта: `local N` уникален только внутри документа, а `symbol_to_node` — глобальный, поэтому документы перетирают друг друга и ссылка в одном файле резолвится в определение из другого.

**Files:**
- Modify: `reviewer/graph/scip.py:12-53` (`parse_scip`)
- Test: `tests/graph/test_scip.py`

**Interfaces:**
- Consumes: ничего от других задач.
- Produces: `parse_scip(index, resolve) -> tuple[set[str], list[tuple[str, str, str]]]` — сигнатура не меняется; меняется только состав рёбер. Задача 4 полагается на то, что рёбра остаются тройками `(src, rel, dst)`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в конец `tests/graph/test_scip.py`:

```python
def _resolver(intervals):
    """Резолвер fqn по интервалам строк: {path: [(fqn, start, end), ...]}."""
    def resolve(path, line1):
        best = None
        for fqn, s, e in intervals.get(path, []):
            if s <= line1 <= e and (best is None or (e - s) < (best[2] - best[1])):
                best = (fqn, s, e)
        return best[0] if best else None
    return resolve


def test_local_symbols_do_not_leak_across_documents():
    """`local 0` в разных файлах — разные символы; ребра между файлами быть не должно."""
    idx = Index()
    a = Document(relative_path="a.py")
    a.occurrences.append(_occ("local 0", 0, DEF))    # определение local 0 в a.py
    b = Document(relative_path="b.py")
    b.occurrences.append(_occ("local 0", 5, DEF))    # то же ИМЯ символа в b.py
    b.occurrences.append(_occ("local 0", 6))         # ссылка внутри b.py
    idx.documents.extend([a, b])

    resolve = _resolver({"a.py": [("f", 1, 4)],
                         "b.py": [("g", 6, 6), ("h", 7, 9)]})
    _, edges = scip.parse_scip(idx, resolve)

    assert ("b.py#h", "CALLS", "a.py#f") not in edges     # кросс-файловая фикция
    assert not [e for e in edges if e[0].startswith("b.py") and e[2].startswith("a.py")]


def test_local_symbol_resolves_within_its_own_document():
    """Внутри одного документа local-символ по-прежнему даёт ребро."""
    idx = Index()
    doc = Document(relative_path="a.py")
    doc.occurrences.append(_occ("local 3", 0, DEF))   # вложенная функция в f
    doc.occurrences.append(_occ("local 3", 7))        # использована в g
    idx.documents.append(doc)

    resolve = _resolver({"a.py": [("f", 1, 4), ("g", 6, 9)]})
    _, edges = scip.parse_scip(idx, resolve)

    assert ("a.py#g", "CALLS", "a.py#f") in edges


def test_global_symbols_still_resolve_across_documents():
    """Регрессия: глобальные символы должны связывать файлы как и раньше."""
    idx = Index()
    a = Document(relative_path="a.py")
    a.occurrences.append(_occ("scip . pkg f().", 0, DEF))
    b = Document(relative_path="b.py")
    b.occurrences.append(_occ("scip . pkg f().", 6))
    idx.documents.extend([a, b])

    resolve = _resolver({"a.py": [("f", 1, 4)], "b.py": [("g", 6, 9)]})
    _, edges = scip.parse_scip(idx, resolve)

    assert ("b.py#g", "CALLS", "a.py#f") in edges
```

- [ ] **Step 2: Запустить тесты и убедиться, что падают**

Run: `.venv/bin/pytest tests/graph/test_scip.py -q`
Expected: FAIL — `test_local_symbols_do_not_leak_across_documents` находит кросс-файловое ребро (`assert not [...]`), остальные два проходят.

- [ ] **Step 3: Реализовать скоупинг**

Заменить тело `parse_scip` в `reviewer/graph/scip.py` (строки 12-53) на:

```python
def _is_local(symbol: str) -> bool:
    """True для файл-скоупного символа SCIP (`local <N>`).

    Такой идентификатор уникален ТОЛЬКО внутри своего документа: `local 0` в
    двух разных файлах — один и тот же ключ. Глобальная карта символов на них
    давала кросс-файловую фикцию (половина CALLS-рёбер, PRI-252).
    """
    return symbol.startswith("local ")


def parse_scip(index, resolve: FqnResolver):
    """index: scip_pb2.Index. Возвращает (nodes:set[str], edges:list[(src,rel,dst)])."""
    nodes: set[str] = set()
    edges: list[tuple[str, str, str]] = []
    symbol_to_node: dict[str, str] = {}                  # глобальные символы
    local_to_node: dict[str, dict[str, str]] = {}        # {документ: {символ: node_id}}

    def lookup(path: str, symbol: str) -> str | None:
        if _is_local(symbol):
            return local_to_node.get(path, {}).get(symbol)
        return symbol_to_node.get(symbol)

    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & DEFINITION:
                fqn = resolve(doc.relative_path, _start_line_1based(occ))
                if fqn:
                    nid = f"{doc.relative_path}#{fqn}"
                    if _is_local(occ.symbol):
                        local_to_node.setdefault(doc.relative_path, {})[occ.symbol] = nid
                    else:
                        symbol_to_node[occ.symbol] = nid
                    nodes.add(nid)

    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & DEFINITION:
                continue
            callee = lookup(doc.relative_path, occ.symbol)
            if callee is None:
                continue
            caller_fqn = resolve(doc.relative_path, _start_line_1based(occ))
            if not caller_fqn:
                continue
            caller = f"{doc.relative_path}#{caller_fqn}"
            if caller != callee:
                nodes.add(caller)
                edges.append((caller, "CALLS", callee))

    for doc in index.documents:
        for si in doc.symbols:
            src = lookup(doc.relative_path, si.symbol)
            if src is None:
                continue
            for rel in si.relationships:
                if rel.is_implementation:
                    dst = lookup(doc.relative_path, rel.symbol)
                    if dst:
                        edges.append((src, "IMPLEMENTS", dst))

    return nodes, list(dict.fromkeys(edges))
```

- [ ] **Step 4: Запустить тесты и убедиться, что проходят**

Run: `.venv/bin/pytest tests/graph/test_scip.py tests/graph/test_backend.py -q`
Expected: PASS (все тесты файла, включая существующие).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/graph/scip.py tests/graph/test_scip.py
git commit -m "fix(graph): резолвить local-символы SCIP в пределах документа"
```

---

### Task 2: Модуль метрик графа — счётчики по типам и детектор просадки

**Files:**
- Create: `reviewer/graph/metrics.py`
- Test: `tests/graph/test_metrics.py`

**Interfaces:**
- Consumes: ничего (чистый модуль без БД и графа).
- Produces:
  - `EDGE_REGRESSION_THRESHOLD: float = 0.10`
  - `count_edges_by_rel(edges: Iterable[tuple[str, str, str]]) -> dict[str, int]`
  - `format_edge_counts(counts: dict[str, int]) -> str` — `"CALLS 17963, IMPLEMENTS 129"`
  - `detect_edge_regression(prev: dict[str, int] | None, curr: dict[str, int], threshold: float = EDGE_REGRESSION_THRESHOLD) -> list[str]`

  Задача 4 вызывает все четыре.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/graph/test_metrics.py`:

```python
"""Метрики полноты графа: счётчики по типам рёбер и детектор просадки (PRI-252)."""
from reviewer.graph.metrics import (
    count_edges_by_rel,
    detect_edge_regression,
    format_edge_counts,
)


def test_count_edges_by_rel_groups_by_relation():
    edges = [
        ("a.py#f", "CALLS", "b.py#g"),
        ("a.py#f", "CALLS", "b.py#h"),
        ("a.py#C", "IMPLEMENTS", "b.py#Base"),
    ]
    assert count_edges_by_rel(edges) == {"CALLS": 2, "IMPLEMENTS": 1}


def test_count_edges_by_rel_empty():
    assert count_edges_by_rel([]) == {}


def test_format_edge_counts_sorts_by_size_desc():
    assert format_edge_counts({"IMPLEMENTS": 129, "CALLS": 17963}) == \
        "CALLS 17963, IMPLEMENTS 129"


def test_format_edge_counts_empty():
    assert format_edge_counts({}) == "нет"


def test_detect_edge_regression_reports_drop_over_threshold():
    msgs = detect_edge_regression({"CALLS": 30254}, {"CALLS": 17963})
    assert msgs == ["CALLS 30254 → 17963 (−41%)"]


def test_detect_edge_regression_silent_within_threshold():
    assert detect_edge_regression({"CALLS": 1000}, {"CALLS": 950}) == []


def test_detect_edge_regression_reports_vanished_type():
    msgs = detect_edge_regression({"CALLS": 100, "IMPLEMENTS": 12}, {"CALLS": 100})
    assert msgs == ["IMPLEMENTS 12 → 0 (−100%)"]


def test_detect_edge_regression_ignores_growth_and_new_types():
    assert detect_edge_regression({"CALLS": 100}, {"CALLS": 200, "IMPLEMENTS": 5}) == []


def test_detect_edge_regression_without_previous_measurement():
    assert detect_edge_regression(None, {"CALLS": 100}) == []


def test_detect_edge_regression_respects_custom_threshold():
    assert detect_edge_regression({"CALLS": 100}, {"CALLS": 95}, threshold=0.01) == \
        ["CALLS 100 → 95 (−5%)"]
```

- [ ] **Step 2: Запустить тесты и убедиться, что падают**

Run: `.venv/bin/pytest tests/graph/test_metrics.py -q`
Expected: FAIL с `ModuleNotFoundError: No module named 'reviewer.graph.metrics'`.

- [ ] **Step 3: Реализовать модуль**

Создать `reviewer/graph/metrics.py`:

```python
"""Метрики полноты графа кода (PRI-252).

Чистые функции без БД и Neo4j: считают рёбра по типам, форматируют разбивку
для вывода `reviewer index` и сравнивают текущий замер с предыдущим замером
той же ветки, чтобы просадка полноты не проходила молча.

Порог — константа модуля, а не env-ключ: остаточное расхождение числа рёбер
между окружениями запуска scip-python ~0.5 %, порог лишь отделяет его от
настоящей потери сигнала.
"""
from __future__ import annotations

import collections
from collections.abc import Iterable

EDGE_REGRESSION_THRESHOLD = 0.10


def count_edges_by_rel(edges: Iterable[tuple[str, str, str]]) -> dict[str, int]:
    """Счётчики рёбер по типу отношения: {"CALLS": N, "IMPLEMENTS": M}."""
    counter: collections.Counter[str] = collections.Counter()
    for _src, rel, _dst in edges:
        counter[rel] += 1
    return dict(counter)


def format_edge_counts(counts: dict[str, int]) -> str:
    """Человекочитаемая разбивка, по убыванию количества: "CALLS 17963, IMPLEMENTS 129"."""
    if not counts:
        return "нет"
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{rel} {n}" for rel, n in items)


def detect_edge_regression(
    prev: dict[str, int] | None,
    curr: dict[str, int],
    threshold: float = EDGE_REGRESSION_THRESHOLD,
) -> list[str]:
    """Сообщения о просадке по типам рёбер относительно предыдущего замера.

    Просадкой считается падение более чем на ``threshold`` долю от предыдущего
    значения, включая полное исчезновение типа. Рост и новые типы молчат.
    Отсутствие предыдущего замера (``None``) — не просадка: сравнивать не с чем.
    """
    if not prev:
        return []
    messages: list[str] = []
    for rel, was in sorted(prev.items()):
        if was <= 0:
            continue
        now = curr.get(rel, 0)
        if now < was * (1 - threshold):
            pct = round((was - now) * 100 / was)
            messages.append(f"{rel} {was} → {now} (−{pct}%)")
    return messages
```

- [ ] **Step 4: Запустить тесты и убедиться, что проходят**

Run: `.venv/bin/pytest tests/graph/test_metrics.py -q`
Expected: PASS (10 тестов).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/graph/metrics.py tests/graph/test_metrics.py
git commit -m "feat(graph): счётчики рёбер по типам и детектор просадки полноты"
```

---

### Task 3: Персист счётчиков рёбер в `index_meta`

**Files:**
- Modify: `reviewer/index/schema.sql:40-53` (блок `index_meta`)
- Modify: `reviewer/index/store.py:186-213` (рядом с `get_index_meta`/`set_index_meta`)
- Test: `tests/index/test_index_meta_edges.py`

**Interfaces:**
- Consumes: ничего от других задач.
- Produces (методы `ChunkStore`):
  - `set_graph_edge_counts(self, repo: str, ref: str, counts: dict[str, int]) -> None`
  - `get_graph_edge_counts(self, repo: str, ref: str) -> dict[str, int] | None`

  Задача 4 вызывает оба. Счётчики пишутся ОТДЕЛЬНЫМ методом, а не параметром
  `set_index_meta`, потому что SHA пишется до построения графа, а счётчики
  известны только после него.

- [ ] **Step 1: Написать падающий integration-тест**

Создать `tests/index/test_index_meta_edges.py`:

```python
"""Персист счётчиков рёбер графа в index_meta (PRI-252)."""
from uuid import uuid4

import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore


@pytest.mark.integration
def test_graph_edge_counts_round_trip():
    settings = Settings()
    repo = f"test/edge-counts-{uuid4().hex}"
    store = ChunkStore(settings.pg_dsn)
    try:
        store.init_schema()
        # Замера ещё не было — сравнивать не с чем.
        assert store.get_graph_edge_counts(repo, "base:main") is None

        store.set_index_meta(repo, "base:main", "cafe1234")
        store.set_graph_edge_counts(repo, "base:main", {"CALLS": 17963, "IMPLEMENTS": 129})
        assert store.get_graph_edge_counts(repo, "base:main") == \
            {"CALLS": 17963, "IMPLEMENTS": 129}

        # Повторная запись перетирает предыдущий замер.
        store.set_graph_edge_counts(repo, "base:main", {"CALLS": 10})
        assert store.get_graph_edge_counts(repo, "base:main") == {"CALLS": 10}

        # SHA при этом не теряется.
        row = store.get_index_meta_row(repo, "base:main")
        assert row is not None and row[0] == "cafe1234"

        # Другая ветка того же репо изолирована.
        assert store.get_graph_edge_counts(repo, "base:dev") is None
    finally:
        store.clear(repo)
        store.close()


@pytest.mark.integration
def test_init_schema_is_idempotent_for_edge_counts():
    """Повторный init_schema не ломает уже записанные счётчики."""
    settings = Settings()
    repo = f"test/edge-counts-idem-{uuid4().hex}"
    store = ChunkStore(settings.pg_dsn)
    try:
        store.init_schema()
        store.set_index_meta(repo, "base:main", "cafe1234")
        store.set_graph_edge_counts(repo, "base:main", {"CALLS": 5})
        store.init_schema()
        assert store.get_graph_edge_counts(repo, "base:main") == {"CALLS": 5}
    finally:
        store.clear(repo)
        store.close()
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `.venv/bin/pytest tests/index/test_index_meta_edges.py -q -m integration`
Expected: FAIL с `AttributeError: 'ChunkStore' object has no attribute 'get_graph_edge_counts'`.

(Если тестовая инфраструктура не поднята: `docker compose --profile test up -d --wait paradedb-test neo4j-test`.)

- [ ] **Step 3: Добавить колонку в схему**

В `reviewer/index/schema.sql`, сразу после строки
`ALTER TABLE index_meta ADD PRIMARY KEY (repo, ref);`, дописать:

```sql
-- Счётчики рёбер графа последнего прогона по (repo, ref): {"CALLS": N, "IMPLEMENTS": M}.
-- Нужны, чтобы просадка полноты графа не проходила молча (PRI-252). Nullable:
-- на индексе, построенном старой версией, предыдущего замера просто нет.
ALTER TABLE index_meta ADD COLUMN IF NOT EXISTS graph_edges JSONB;
```

- [ ] **Step 4: Добавить методы стора**

В `reviewer/index/store.py` дописать сразу после `set_index_meta` (строка 213):

```python
    def get_graph_edge_counts(self, repo: str, ref: str) -> dict[str, int] | None:
        """Счётчики рёбер графа предыдущего прогона для ref, или None.

        None означает «сравнивать не с чем»: записи нет, либо индекс построен
        версией без этой колонки/таблицы — как и в get_index_meta, отсутствие
        схемы не вправе ронять индексацию."""
        import psycopg.errors
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT graph_edges FROM index_meta WHERE repo=%s AND ref=%s",
                    (repo, ref),
                ).fetchone()
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            return None
        return row[0] if row and row[0] else None

    def set_graph_edge_counts(self, repo: str, ref: str, counts: dict[str, int]) -> None:
        """Записать счётчики рёбер графа для ref.

        Отдельно от set_index_meta: SHA известен до построения графа, счётчики —
        только после. Строка к этому моменту уже создана set_index_meta."""
        from psycopg.types.json import Json
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO index_meta (repo, ref, sha, graph_edges, updated_at)
                VALUES (%s, %s, '', %s, now())
                ON CONFLICT (repo, ref) DO UPDATE SET graph_edges = EXCLUDED.graph_edges
                """,
                (repo, ref, Json(counts)),
            )
            conn.commit()
```

- [ ] **Step 5: Запустить тесты и убедиться, что проходят**

Run: `.venv/bin/pytest tests/index/test_index_meta_edges.py tests/index/test_status_meta.py -q -m integration`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/index/schema.sql reviewer/index/store.py tests/index/test_index_meta_edges.py
git commit -m "feat(index): персист счётчиков рёбер графа в index_meta"
```

---

### Task 4: Проводка в `reviewer index` — разбивка и предупреждение о просадке

**Files:**
- Modify: `reviewer/entrypoints/cli.py:1012-1026` (блок построения графа в команде `index`)
- Test: `tests/entrypoints/test_index_edge_metrics.py`

**Interfaces:**
- Consumes: `count_edges_by_rel`, `format_edge_counts`, `detect_edge_regression` из Задачи 2; `get_graph_edge_counts`, `set_graph_edge_counts` из Задачи 3; тройки рёбер из Задачи 1.
- Produces: строку итога вида
  `граф [scip]: узлов 7270, рёбер 18092 (CALLS 17963, IMPLEMENTS 129)`
  и, при просадке, строки
  `⚠ Просадка полноты графа против предыдущего индекса ветки: CALLS 30254 → 17963 (−41%)`.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/entrypoints/test_index_edge_metrics.py`:

```python
"""reviewer index: разбивка рёбер по типам и предупреждение о просадке (PRI-252)."""
from __future__ import annotations

from unittest.mock import MagicMock

from click.testing import CliRunner

import reviewer.entrypoints.cli as cli_mod
from reviewer.services.repo_id import RepoResolution

EDGES = [
    ("reviewer/a.py#f", "CALLS", "reviewer/b.py#g"),
    ("reviewer/a.py#f", "CALLS", "reviewer/b.py#h"),
    ("reviewer/a.py#C", "IMPLEMENTS", "reviewer/b.py#Base"),
]


def _wire(monkeypatch, components, edges=EDGES):
    monkeypatch.setenv("REVIEW_BRANCHES", "main,dev")
    monkeypatch.setattr(cli_mod, "build_components", lambda s: components)
    monkeypatch.setattr(cli_mod, "_resolve_repo",
                        lambda *a: RepoResolution("o/r", "cli"))
    monkeypatch.setattr(cli_mod, "list_python_files",
                        lambda repo, ref: ["reviewer/a.py"])
    monkeypatch.setattr(cli_mod, "rev_parse", lambda repo, ref: "deadbeef")
    monkeypatch.setattr(cli_mod, "file_at_ref",
                        lambda repo, path, ref: None if path == ".review.yml"
                        else "def f():\n    pass\n")
    monkeypatch.setattr(cli_mod, "update_base", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "build_code_graph",
                        lambda *a, **k: ({"reviewer/a.py#f"}, edges, "scip"))


def test_index_prints_edge_breakdown(monkeypatch):
    components = MagicMock()
    components.store.get_graph_edge_counts.return_value = None
    _wire(monkeypatch, components)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "main"])

    assert result.exit_code == 0, result.output
    assert "рёбер 3 (CALLS 2, IMPLEMENTS 1)" in result.output
    components.store.set_graph_edge_counts.assert_called_once_with(
        "o/r", "base:main", {"CALLS": 2, "IMPLEMENTS": 1})


def test_index_warns_on_edge_regression(monkeypatch):
    components = MagicMock()
    components.store.get_graph_edge_counts.return_value = {"CALLS": 100, "IMPLEMENTS": 1}
    _wire(monkeypatch, components)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "main"])

    assert result.exit_code == 0, result.output          # предупреждение не роняет команду
    assert "Просадка полноты графа" in result.output
    assert "CALLS 100 → 2 (−98%)" in result.output
    assert "IMPLEMENTS" not in result.output.split("Просадка полноты графа")[1]


def test_index_silent_without_previous_measurement(monkeypatch):
    components = MagicMock()
    components.store.get_graph_edge_counts.return_value = None
    _wire(monkeypatch, components)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "main"])

    assert result.exit_code == 0, result.output
    assert "Просадка" not in result.output


def test_index_survives_failed_counter_write(monkeypatch):
    """Счётчики вторичны: их запись не вправе ронять уже построенный индекс."""
    components = MagicMock()
    components.store.get_graph_edge_counts.return_value = None
    components.store.set_graph_edge_counts.side_effect = RuntimeError("нет колонки")
    _wire(monkeypatch, components)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "main"])

    assert result.exit_code == 0, result.output
    assert "рёбер 3 (CALLS 2, IMPLEMENTS 1)" in result.output


def test_index_survives_failed_counter_read(monkeypatch):
    """Недоступный предыдущий замер — не повод падать: печатаем разбивку без сравнения."""
    components = MagicMock()
    components.store.get_graph_edge_counts.side_effect = RuntimeError("нет колонки")
    _wire(monkeypatch, components)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "main"])

    assert result.exit_code == 0, result.output
    assert "рёбер 3 (CALLS 2, IMPLEMENTS 1)" in result.output
    assert "Просадка" not in result.output
```

- [ ] **Step 2: Запустить тесты и убедиться, что падают**

Run: `.venv/bin/pytest tests/entrypoints/test_index_edge_metrics.py -q`
Expected: FAIL — в выводе нет `рёбер 3 (CALLS 2, IMPLEMENTS 1)` (сейчас печатается `рёбер 3` без разбивки).

- [ ] **Step 3: Добавить импорт метрик**

В `reviewer/entrypoints/cli.py` к существующему импорту `build_code_graph` добавить рядом:

```python
from reviewer.graph.metrics import (
    count_edges_by_rel,
    detect_edge_regression,
    format_edge_counts,
)
```

(Если `build_code_graph` импортируется внутри функции — положить импорт метрик туда же, рядом.)

- [ ] **Step 4: Проводка в команде `index`**

В `reviewer/entrypoints/cli.py` заменить блок графа (от `gnodes, gedges, backend = build_code_graph(` до закрывающего `)` вызова `click.echo`) на:

```python
        # Предыдущий замер полноты графа читается ДО перестройки: сравнивать
        # нужно с тем, что стояло в индексе этой ветки раньше (PRI-252).
        # Fail-soft: недоступный замер значит «сравнивать не с чем», а не отказ.
        try:
            prev_edges = c.store.get_graph_edge_counts(repo_id, bref)
        except Exception:  # noqa: BLE001 — диагностика вторична к индексации
            log.warning("Не удалось прочитать предыдущие счётчики рёбер для %s", repo_id)
            prev_edges = None
        gnodes, gedges, backend = build_code_graph(
            repo, ref, files, src_by_path, s.graph_backend,
        )
        c.graph.init_schema()
        c.graph.clear(repo_id, branch=branch)   # rebuild только этой ветки репо
        c.graph.upsert_nodes(repo_id, list(gnodes), branch=branch)
        c.graph.upsert_edges(repo_id, gedges, branch=branch)
        edge_counts = count_edges_by_rel(gedges)
        try:
            c.store.set_graph_edge_counts(repo_id, bref, edge_counts)
        except Exception:  # noqa: BLE001 — граф уже записан, счётчики вторичны
            log.warning("Не удалось записать счётчики рёбер для %s", repo_id)
        click.echo(
            f"Проиндексировано [{repo_id}@{branch}] файлов: {len(files)} @ {sha[:7]}; "
            f"граф [{backend}]: узлов {len(gnodes)}, рёбер {len(gedges)} "
            f"({format_edge_counts(edge_counts)})"
        )
        for message in detect_edge_regression(prev_edges, edge_counts):
            click.echo(f"⚠ Просадка полноты графа против предыдущего индекса ветки: {message}")
            log.warning("Просадка полноты графа [%s@%s]: %s", repo_id, branch, message)
```

- [ ] **Step 5: Запустить тесты и убедиться, что проходят**

Run: `.venv/bin/pytest tests/entrypoints/ -q`
Expected: PASS (новый файл + существующие тесты команды `index`).

- [ ] **Step 6: Полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 7: Коммит**

```bash
git add reviewer/entrypoints/cli.py tests/entrypoints/test_index_edge_metrics.py
git commit -m "feat(cli): разбивка рёбер по типам и предупреждение о просадке графа"
```

---

### Task 5: Integration-тест на реальном scip-python и факт в CLAUDE.md

**Files:**
- Modify: `tests/graph/test_backend_integration.py` (дописать тест в конец)
- Modify: `CLAUDE.md` (блок «Граф кода — два бэкенда»)

**Interfaces:**
- Consumes: `parse_scip` из Задачи 1 через `build_with_scip`.
- Produces: ничего для последующих задач (финальная).

- [ ] **Step 1: Написать падающий integration-тест**

Дописать в конец `tests/graph/test_backend_integration.py`. Все нужные импорты (`os`, `shutil`, `subprocess`, `pytest`, `build_with_scip`) и хелпер `_init_repo` в файле уже есть — шапку не трогать.

```python
@pytest.mark.integration
@pytest.mark.skipif(shutil.which("scip-python") is None,
                    reason="scip-python не установлен")
def test_scip_does_not_link_files_through_local_symbols(tmp_path):
    """Одноимённые локальные переменные в разных файлах не связывают их рёбрами.

    До PRI-252 файл-скоупные символы SCIP (`local N`) резолвились через общую
    карту, и ссылка на локальное имя в одном файле попадала в определение из
    другого. Внутрифайловое ребро при этом обязано сохраниться.
    """
    repo = str(tmp_path / "repo")
    os.mkdir(repo)
    files = {
        "a.py": "def alpha():\n    helper = 1\n    return helper\n",
        "b.py": "def beta():\n    helper = 2\n    return helper\n",
        "pyproject.toml": '[project]\nname = "probe"\nversion = "0.0.1"\n',
    }
    _init_repo(repo, files)

    src = {p: s for p, s in files.items() if p.endswith(".py")}
    _nodes, edges = build_with_scip(repo, "HEAD", src)

    cross = [e for e in edges
             if (e[0].startswith("a.py") and e[2].startswith("b.py"))
             or (e[0].startswith("b.py") and e[2].startswith("a.py"))]
    assert cross == [], f"кросс-файловые рёбра из local-символов: {cross}"
```

- [ ] **Step 2: Запустить тест**

Run: `.venv/bin/pytest tests/graph/test_backend_integration.py -q -m integration`
Expected: PASS (после Задачи 1). Если Задача 1 откатить — тест падает; это и есть его смысл.

- [ ] **Step 3: Записать неочевидный факт в CLAUDE.md**

В `CLAUDE.md`, в раздел «Неочевидные факты», сразу после пункта «**Наследование классов в графе приходит из tree-sitter, а не из SCIP (PRI-251).**», вставить:

```markdown
- **`local N`-символы SCIP файл-скоупные, и глобальная карта давала фикцию (PRI-252).**
  Идентификатор `local <N>` в index.scip уникален только внутри своего документа:
  `local 0` в `reviewer/app.py` и `local 0` в `tests/web/test_pool.py` — один и тот
  же ключ. `parse_scip` до PRI-252 клал все definition-символы в общий
  `symbol_to_node`, документы перетирали друг друга, и ссылка на локальное имя в
  одном файле резолвилась в определение из другого. Измерено на этом репозитории:
  14942 фиктивных ребра `reviewer/* → tests/*` из 30254 CALLS — 49 %. Теперь
  `local`-символы резолвятся в пределах своего документа (`_is_local` в
  `graph/scip.py`); внутрифайловые рёбра при этом сохраняются.
  Побочное следствие того же дефекта — число рёбер зависело от окружения запуска
  `scip-python`: worktree своего окружения не имеет, pyright резолвит типы по
  окружению вызывающего процесса, и чем хуже резолв, тем больше символов остаются
  `local`. На одном коммите `.venv` проекта давал 30254 CALLS, системный python —
  32832, `uvx` — 34158, каждое значение детерминировано в своём условии. После
  фикса остаточное расхождение — 0.5 % (17776 против 17683); поэтому детектор
  просадки (`graph/metrics.py`) сравнивает с порогом 10 %, а не на равенство.
```

- [ ] **Step 4: Проверить, что документация не разошлась с кодом**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewer/ tests/`
Expected: тесты PASS; ruff — без новых замечаний по изменённым файлам (репозиторий не обязан быть чистым целиком).

- [ ] **Step 5: Коммит**

```bash
git add tests/graph/test_backend_integration.py CLAUDE.md
git commit -m "docs(graph): зафиксировать файл-скоупность local-символов SCIP как неочевидный факт"
```

---

## Проверка после плана (вручную, без коммита)

- [ ] Перестроить индекс и убедиться, что вывод содержит разбивку и одноразовое предупреждение о просадке (ожидаемое: удаление фикции, ~34287 → ~18100):

```bash
.venv/bin/reviewer index . --ref dev --repo mimfort/rag_for_git
```

- [ ] Повторить команду ещё раз — предупреждения быть не должно (предыдущий замер уже новый).
